r"""Forge and NeoForge, via the official installer run headlessly.

Forge does not publish a ready-made version JSON the way Fabric does. Its installer runs a
*processor pipeline* -- it patches the vanilla client jar, generates several libraries, and
only then writes the version JSON. Reimplementing that pipeline is a legitimate v2 goal and
a real project on its own; for v1 we shell out::

    <the java that Minecraft version needs> -jar forge-<ver>-installer.jar --installClient <dir>

Three traps, each of which costs an hour the first time:

1. **The installer requires ``launcher_profiles.json`` to already exist** in the target
   directory or it aborts. We keep our own state, so a stub ``{"profiles":{},"version":3}``
   is written first and the profile the installer injects into it is then ignored. We never
   write to the *official* launcher's copy -- it rewrites that file from memory when it
   exits and would silently erase anything we put there (trap 12.1).
2. **Run the installer under the right Java.** The 1.12.2 installer wants Java 8; handing
   it Java 21 fails inside the processors with a class file version error that reads like a
   Forge bug.
3. **The vanilla client jar must already be installed**, because the processors patch it.
   Installing Forge before the version it inherits from produces a cryptic failure about a
   missing input.

``--installClient <dir>`` treats ``<dir>`` as a launcher root, writing into ``<dir>/versions``
and ``<dir>/libraries``. Those are exactly our shared directories, so the data root is
passed straight in and the output lands where the resolver already looks.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path

from .. import java as java_mod
from .. import logs, net, versions
from ..paths import Layout, ext, mkdirs

log = logs.get("loaders.forge")

FORGE_MAVEN = "https://maven.minecraftforge.net/net/minecraftforge/forge"
FORGE_PROMOS = ("https://files.minecraftforge.net/net/minecraftforge/forge/"
                "promotions_slim.json")
NEOFORGE_MAVEN = "https://maven.neoforged.net/releases/net/neoforged/neoforge"
NEOFORGE_VERSIONS = ("https://maven.neoforged.net/api/maven/versions/releases/"
                     "net/neoforged/neoforge")


class ForgeError(RuntimeError):
    pass


@dataclass(frozen=True)
class Flavour:
    key: str
    label: str
    maven: str


FORGE = Flavour("forge", "Forge", FORGE_MAVEN)
NEOFORGE = Flavour("neoforge", "NeoForge", NEOFORGE_MAVEN)


# ---------------------------------------------------------------------------------
# Version discovery
# ---------------------------------------------------------------------------------

def _maven_metadata_versions(maven_base: str) -> list[str]:
    raw = net.get_bytes(f"{maven_base}/maven-metadata.xml")
    root = ET.fromstring(raw.decode("utf-8", "replace"))
    return [e.text or "" for e in root.iterfind(".//versions/version")]


def forge_versions(mc_version: str) -> list[str]:
    """Forge build numbers for a Minecraft version, newest first.

    Forge's maven keys entries as ``<mc>-<forge>``; a couple of old ones carry a trailing
    branch suffix (``1.7.10-10.13.4.1614-1.7.10``), which is stripped so the build number
    is what the caller sees.
    """
    prefix = f"{mc_version}-"
    out: list[str] = []
    for full in _maven_metadata_versions(FORGE_MAVEN):
        if not full.startswith(prefix):
            continue
        rest = full[len(prefix):]
        if rest.endswith(f"-{mc_version}"):
            rest = rest[: -len(mc_version) - 1]
        out.append(rest)
    out.reverse()
    return out


def forge_recommended(mc_version: str) -> str | None:
    try:
        promos = net.get_json(FORGE_PROMOS).get("promos") or {}
    except net.NetError:
        return None
    return promos.get(f"{mc_version}-recommended") or promos.get(f"{mc_version}-latest")


_NEO_RE = re.compile(r"^(\d+)\.(\d+)\.")


def neoforge_versions(mc_version: str) -> list[str]:
    """NeoForge versions for a Minecraft version, newest first.

    NeoForge numbers builds ``<minor>.<patch>.<build>`` derived from the Minecraft version
    rather than embedding it: 1.20.1 is the odd one out (it used ``47.1.x`` under the old
    scheme), everything from 1.20.2 on maps ``1.X.Y`` to ``X.Y.*`` and ``1.X`` to ``X.0.*``.
    """
    parts = mc_version.split(".")
    if len(parts) < 2 or parts[0] != "1":
        return []
    want = f"{parts[1]}.{parts[2] if len(parts) > 2 else '0'}."
    try:
        data = net.get_json(NEOFORGE_VERSIONS)
    except net.NetError:
        return []
    out = [v for v in (data.get("versions") or []) if v.startswith(want)]
    out.reverse()
    return out


def available(flavour: Flavour, mc_version: str) -> list[str]:
    return (forge_versions if flavour is FORGE else neoforge_versions)(mc_version)


def installer_url(flavour: Flavour, mc_version: str, loader_version: str) -> str:
    if flavour is FORGE:
        full = f"{mc_version}-{loader_version}"
        return f"{FORGE_MAVEN}/{full}/forge-{full}-installer.jar"
    return (f"{NEOFORGE_MAVEN}/{loader_version}"
            f"/neoforge-{loader_version}-installer.jar")


def expected_version_id(flavour: Flavour, mc_version: str, loader_version: str) -> str:
    if flavour is FORGE:
        return f"{mc_version}-forge-{loader_version}"
    return f"neoforge-{loader_version}"


# ---------------------------------------------------------------------------------
# Installation
# ---------------------------------------------------------------------------------

STUB_PROFILES = {"profiles": {}, "version": 3}


def _write_profiles_stub(root: Path) -> Path:
    """Trap 1. The installer refuses to run without this file existing."""
    path = root / "launcher_profiles.json"
    if not path.is_file():
        net.write_atomic(path, json.dumps(STUB_PROFILES, indent=2).encode("utf-8"))
        log.debug("wrote launcher_profiles.json stub at %s", path)
    # Some installer builds also probe the microsoft-store variant.
    alt = root / "launcher_profiles_microsoft_store.json"
    if not alt.is_file():
        net.write_atomic(alt, json.dumps(STUB_PROFILES, indent=2).encode("utf-8"))
    return path


def _ensure_vanilla_base(layout: Layout, mc_version: str,
                         progress: net.Progress | None,
                         cancel: net.CancelToken) -> None:
    """Trap 3. The processors patch the vanilla client jar, so it has to be there."""
    from .. import libraries as lib_mod
    if progress:
        progress.set_phase("Preparing base game", mc_version)
    version = versions.resolve(layout, mc_version, cancel=cancel)
    jobs, _ = lib_mod.plan_downloads(version, layout)
    client = (version.get("downloads") or {}).get("client") or {}
    dest = layout.version_jar(mc_version)
    if client.get("url") and not net.verify(dest, client.get("sha1"), client.get("size")):
        jobs.append(net.Job(client["url"], dest, client.get("sha1"), client.get("size"),
                            label=f"{mc_version}.jar"))
    if jobs:
        net.run_jobs(jobs, progress=progress, phase="Downloading base game",
                     cancel=cancel)


def install(layout: Layout, mc_version: str, *, loader_version: str | None = None,
            flavour: Flavour = FORGE, progress: net.Progress | None = None,
            cancel: net.CancelToken = net.NEVER, timeout: float = 900.0) -> str:
    """Install Forge/NeoForge headlessly and return the launchable version id."""
    if loader_version is None:
        if flavour is FORGE:
            loader_version = forge_recommended(mc_version)
        if loader_version is None:
            candidates = available(flavour, mc_version)
            if not candidates:
                raise ForgeError(
                    f"{flavour.label} publishes no build for Minecraft {mc_version}.")
            loader_version = candidates[0]

    version_id = expected_version_id(flavour, mc_version, loader_version)
    if layout.version_json(version_id).is_file():
        log.info("%s %s already installed", flavour.label, version_id)
        return version_id

    _ensure_vanilla_base(layout, mc_version, progress, cancel)

    url = installer_url(flavour, mc_version, loader_version)
    jar = layout.meta_cache / "installers" / Path(url).name
    if progress:
        progress.set_phase(f"Downloading {flavour.label} installer", loader_version)
    mkdirs(jar.parent)
    net.download(url, jar, cancel=cancel,
                 on_bytes=progress.add_bytes if progress else None)

    _write_profiles_stub(layout.data_root)

    # Trap 2: the installer runs under the Java that Minecraft version needs.
    base_version = versions.resolve(layout, mc_version, cancel=cancel)
    java_exe = java_mod.resolve_java(layout, base_version, progress=progress,
                                     cancel=cancel)

    if progress:
        progress.set_phase(f"Running {flavour.label} installer",
                           "this takes a minute and is mostly silent")
    argv = [str(java_exe), "-jar", str(jar), "--installClient", str(layout.data_root)]
    log.info("running %s installer: %s", flavour.label, " ".join(map(repr, argv)))

    try:
        proc = subprocess.run(
            argv,                       # an array; the data root contains a space
            cwd=str(layout.data_root),
            capture_output=True, text=True, timeout=timeout,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except subprocess.TimeoutExpired as exc:
        raise ForgeError(
            f"The {flavour.label} installer did not finish within {timeout:.0f}s."
        ) from exc
    except OSError as exc:
        raise ForgeError(f"Could not run the {flavour.label} installer: {exc}") from exc

    output = (proc.stdout or "") + (proc.stderr or "")
    if proc.returncode != 0:
        tail = "\n".join(output.strip().splitlines()[-25:])
        raise ForgeError(
            f"The {flavour.label} installer exited with code {proc.returncode}.\n{tail}")

    produced = layout.version_json(version_id)
    if not produced.is_file():
        found = _find_installed_id(layout, mc_version, loader_version, flavour)
        if found is None:
            tail = "\n".join(output.strip().splitlines()[-25:])
            raise ForgeError(
                f"The {flavour.label} installer reported success but wrote no version "
                f"JSON for {version_id}.\n{tail}")
        version_id = found

    log.info("installed %s %s", flavour.label, version_id)
    return version_id


def _find_installed_id(layout: Layout, mc_version: str, loader_version: str,
                       flavour: Flavour) -> str | None:
    """Fall back to matching on disk when the id naming differs from our guess.

    Forge's id format has changed more than once across its lifetime (``1.12.2-forge1.12.2-
    14.23.5.2859`` in the old scheme, ``1.20.1-forge-47.4.10`` in the new one), so the
    reliable answer is what actually appeared.
    """
    needle = loader_version.lower()
    best: str | None = None
    for vid in versions.local_version_ids(layout):
        low = vid.lower()
        if needle in low and (flavour.key in low or mc_version in low):
            if best is None or len(vid) < len(best):
                best = vid
    return best
