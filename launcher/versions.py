"""Version manifest, version JSON, and the ``inheritsFrom`` merge.

The merge is the piece that makes modded versions work at all. A file like
``1.20.1-forge-47.4.10.json`` is a *patch*: it declares ``"inheritsFrom": "1.20.1"`` and
carries only what it changes. Merge order is a correctness requirement, not a style
preference (brief section 4.2, trap 4):

* ``libraries`` -- **child first, then parent**, de-duplicated keeping the first
  occurrence. Forge ships patched copies of vanilla classes and they must win the
  classpath. Getting this backwards produces a game that starts and then behaves like
  vanilla, or crashes deep inside a transformer.
* ``arguments.game`` / ``arguments.jvm`` -- **parent first, child appended.**
* Scalars (``mainClass``, ``minecraftArguments``, ``type``, ``mainClass``) -- child wins
  where it says anything at all.
* ``assetIndex``, ``downloads``, ``javaVersion``, ``assets`` -- normally only the parent
  has them, so they are inherited.

De-duplication keys on ``group:artifact:classifier`` rather than ``group:artifact``,
because in the post-1.19 native layout ``org.lwjgl:lwjgl:3.3.1`` and
``org.lwjgl:lwjgl:3.3.1:natives-windows`` are two different jars that must both survive.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from . import logs, net
from .paths import Layout, ext, mkdirs

log = logs.get("versions")

MANIFEST_URL = "https://launchermeta.mojang.com/mc/game/version_manifest_v2.json"
MANIFEST_TTL = 60 * 60  # an hour; the file is small and new snapshots appear often

#: Key added by :func:`resolve` naming the vanilla version at the bottom of the chain.
#: Underscore-prefixed so it can never collide with a real Mojang field.
ROOT_ID_KEY = "_myfire_root_id"


class VersionError(RuntimeError):
    pass


# ---------------------------------------------------------------------------------
# Maven coordinates
# ---------------------------------------------------------------------------------

@dataclass(frozen=True)
class Maven:
    group: str
    artifact: str
    version: str
    classifier: str | None = None
    extension: str = "jar"

    @classmethod
    def parse(cls, name: str) -> "Maven":
        """``group:artifact:version[:classifier][@ext]``."""
        at = name.rsplit("@", 1)
        ext_ = "jar"
        if len(at) == 2 and "/" not in at[1] and ":" not in at[1]:
            name, ext_ = at[0], at[1]
        parts = name.split(":")
        if len(parts) < 3:
            raise VersionError(f"not a maven coordinate: {name!r}")
        group, artifact, version = parts[0], parts[1], parts[2]
        classifier = parts[3] if len(parts) > 3 else None
        return cls(group, artifact, version, classifier, ext_)

    @property
    def dedupe_key(self) -> tuple[str, str, str]:
        return (self.group, self.artifact, self.classifier or "")

    def path(self) -> str:
        """Relative path inside ``libraries/``, using forward slashes."""
        tail = f"{self.artifact}-{self.version}"
        if self.classifier:
            tail += f"-{self.classifier}"
        return "/".join(self.group.split(".")) + \
            f"/{self.artifact}/{self.version}/{tail}.{self.extension}"

    def __str__(self) -> str:
        s = f"{self.group}:{self.artifact}:{self.version}"
        if self.classifier:
            s += f":{self.classifier}"
        return s


# ---------------------------------------------------------------------------------
# Manifest
# ---------------------------------------------------------------------------------

@dataclass(frozen=True)
class VersionEntry:
    id: str
    type: str            # release | snapshot | old_beta | old_alpha
    url: str
    sha1: str
    release_time: str

    @property
    def year(self) -> str:
        return self.release_time[:4]


@dataclass
class Manifest:
    latest_release: str
    latest_snapshot: str
    versions: list[VersionEntry]

    def get(self, version_id: str) -> VersionEntry | None:
        for v in self.versions:
            if v.id == version_id:
                return v
        return None

    def filtered(self, types: Iterable[str] | None = None) -> list[VersionEntry]:
        if not types:
            return list(self.versions)
        wanted = set(types)
        return [v for v in self.versions if v.type in wanted]


def load_manifest(layout: Layout, *, refresh: bool = False,
                  offline_ok: bool = True) -> Manifest:
    """Fetch the version manifest, cached for an hour, with an offline fallback."""
    cache = layout.meta_cache / "version_manifest_v2.json"
    fresh_enough = False
    try:
        age = time.time() - cache.stat().st_mtime
        fresh_enough = age < MANIFEST_TTL
    except OSError:
        pass

    data: dict | None = None
    if not refresh and fresh_enough:
        data = _read_json(cache)
    if data is None:
        try:
            raw = net.get_bytes(MANIFEST_URL)
            mkdirs(cache.parent)
            net.write_atomic(cache, raw)
            data = json.loads(raw.decode("utf-8"))
        except net.NetError:
            if not offline_ok:
                raise
            data = _read_json(cache)
            if data is None:
                raise
            log.warning("using cached version manifest (network unavailable)")

    latest = data.get("latest", {})
    versions = [
        VersionEntry(v["id"], v.get("type", "release"), v["url"],
                     v.get("sha1", ""), v.get("releaseTime", ""))
        for v in data.get("versions", [])
    ]
    return Manifest(latest.get("release", ""), latest.get("snapshot", ""), versions)


def _read_json(path: Path) -> dict | None:
    try:
        with open(ext(path), "r", encoding="utf-8-sig") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


# ---------------------------------------------------------------------------------
# Version JSON
# ---------------------------------------------------------------------------------

def local_version_ids(layout: Layout) -> list[str]:
    """Version ids already installed on disk (vanilla, Fabric, Forge, anything)."""
    out = []
    try:
        for d in sorted(layout.versions.iterdir()):
            if (d / f"{d.name}.json").is_file():
                out.append(d.name)
    except OSError:
        pass
    return out


def fetch_version_json(layout: Layout, version_id: str, *,
                       manifest: Manifest | None = None,
                       cancel: net.CancelToken = net.NEVER) -> dict:
    """Return a version's raw JSON, from disk if present, else downloaded and verified.

    Version JSONs are immutable, so a cached copy is authoritative. A locally installed
    modded version (Forge, Fabric) exists only on disk and is found the same way.
    """
    on_disk = layout.version_json(version_id)
    data = _read_json(on_disk)
    if data is not None:
        return data

    manifest = manifest or load_manifest(layout)
    entry = manifest.get(version_id)
    if entry is None:
        raise VersionError(
            f"Unknown version {version_id!r}. It is not in Mojang's manifest and is not "
            f"installed at {on_disk.parent}.")

    cancel.check()
    raw = net.get_bytes(entry.url)
    if entry.sha1:
        import hashlib
        actual = hashlib.sha1(raw).hexdigest()
        if actual.lower() != entry.sha1.lower():
            raise net.HashMismatch(on_disk, entry.sha1, actual)
    mkdirs(on_disk.parent)
    net.write_atomic(on_disk, raw)
    return json.loads(raw.decode("utf-8"))


# ---------------------------------------------------------------------------------
# The merge
# ---------------------------------------------------------------------------------

_SCALAR_KEYS = ("mainClass", "minecraftArguments", "type", "assets",
                "complianceLevel", "minimumLauncherVersion", "releaseTime", "time")
_INHERITED_OBJECTS = ("assetIndex", "downloads", "javaVersion", "logging")


def merge(child: dict, parent: dict) -> dict:
    """Apply ``child`` on top of ``parent``. Pure -- no IO, which is what makes it testable."""
    out: dict[str, Any] = dict(parent)

    # id always comes from the child: it names the thing we are launching.
    out["id"] = child.get("id", parent.get("id"))

    for key in _SCALAR_KEYS:
        if child.get(key) is not None:
            out[key] = child[key]

    for key in _INHERITED_OBJECTS:
        if child.get(key) is not None:
            out[key] = child[key]
        elif parent.get(key) is not None:
            out[key] = parent[key]

    # libraries: child first, parent second, first occurrence wins.
    merged_libs: list[dict] = []
    seen: set[tuple[str, str, str]] = set()
    for lib in list(child.get("libraries") or []) + list(parent.get("libraries") or []):
        name = lib.get("name")
        if not name:
            merged_libs.append(lib)
            continue
        try:
            key = Maven.parse(name).dedupe_key
        except VersionError:
            merged_libs.append(lib)
            continue
        if key in seen:
            continue
        seen.add(key)
        merged_libs.append(lib)
    out["libraries"] = merged_libs

    # arguments: parent first, child appended.
    p_args = parent.get("arguments") or {}
    c_args = child.get("arguments") or {}
    if p_args or c_args:
        out["arguments"] = {
            "game": list(p_args.get("game") or []) + list(c_args.get("game") or []),
            "jvm": list(p_args.get("jvm") or []) + list(c_args.get("jvm") or []),
        }

    # Anything else the child declares and we have no rule for: child wins.
    for key, value in child.items():
        if key in ("libraries", "arguments", "inheritsFrom", "id"):
            continue
        if key in _SCALAR_KEYS or key in _INHERITED_OBJECTS:
            continue
        out[key] = value

    out.pop("inheritsFrom", None)
    return out


def resolve(layout: Layout, version_id: str, *, manifest: Manifest | None = None,
            cancel: net.CancelToken = net.NEVER) -> dict:
    """Fully resolve a version, following ``inheritsFrom`` to the vanilla root.

    Chains can be deeper than two (Forge on top of a Fabric-ish base, or an installer that
    emits an intermediate). A visited set makes a malformed pair fail loudly instead of
    recursing forever.
    """
    manifest = manifest or load_manifest(layout)
    chain: list[dict] = []
    seen: set[str] = set()
    current = version_id
    while True:
        if current in seen:
            raise VersionError(
                f"inheritsFrom loop: {' -> '.join(list(seen) + [current])}")
        seen.add(current)
        data = fetch_version_json(layout, current, manifest=manifest, cancel=cancel)
        chain.append(data)
        parent_id = data.get("inheritsFrom")
        if not parent_id:
            break
        current = parent_id
        if len(chain) > 12:
            raise VersionError(f"inheritsFrom chain too deep from {version_id!r}")

    # chain is [most-derived, ..., root]. Fold from the root upward so each child is
    # applied on top of everything below it.
    resolved = chain[-1]
    root_id = chain[-1].get("id") or current
    for child in reversed(chain[:-1]):
        resolved = merge(child, resolved)
    resolved["id"] = version_id
    # The merge consumes `inheritsFrom`, but the root id is still needed afterwards: the
    # client jar belongs to the *vanilla* version, so every modded version of 1.20.1 can
    # share one copy instead of each keeping a byte-identical duplicate under its own id.
    resolved[ROOT_ID_KEY] = root_id
    return resolved
