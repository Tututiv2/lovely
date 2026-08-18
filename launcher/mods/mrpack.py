r"""Install a Modrinth modpack (``.mrpack``) as a complete, isolated instance.

An ``.mrpack`` is a zip holding ``modrinth.index.json`` plus an optional ``overrides/``
tree. The index names the Minecraft version and loader the pack needs, and lists every
mod as a URL with a SHA-1 -- it does **not** contain the mods themselves, which is why a
120 MB pack downloads as a 30 KB file.

So installing one is: read the index, create an instance at the right version with the
right loader, fetch every file verified against its hash, then copy the overrides on top.

Three things are treated as hostile input, because a ``.mrpack`` is a file from the
internet that tells this launcher what to download and where to write it:

* **Every path is checked** before use. ``path`` fields and zip entry names are rejected if
  absolute, if they contain ``..``, or if they resolve outside the instance directory. A
  pack that tried ``../../../.minecraft/mods/x.jar`` would otherwise reach straight past
  the isolation the launcher exists to provide.
* **Every download host is pinned** to the small set Modrinth actually serves from and the
  handful of Maven hosts packs legitimately reference. An arbitrary URL is refused.
* **Every file is verified** against the SHA-1 in the index before it counts as installed.
"""
from __future__ import annotations

import json
import shutil
import urllib.parse
import zipfile
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from .. import logs, net
from ..instances import Instance, InstanceStore, guard_game_dir
from ..paths import Layout, ext, mkdirs

log = logs.get("mods.mrpack")

INDEX_NAME = "modrinth.index.json"
OVERRIDE_DIRS = ("overrides", "client-overrides")

#: Hosts a pack may pull files from. Modrinth's own CDN plus the Maven repositories that
#: real packs reference for loader-adjacent artefacts.
ALLOWED_HOSTS = (
    "cdn.modrinth.com", "cdn-raw.modrinth.com",
    "maven.minecraftforge.net", "maven.neoforged.net",
    "maven.fabricmc.net", "maven.quiltmc.org",
    "libraries.minecraft.net", "api.modrinth.com",
)

#: index -> our loader names.
LOADER_KEYS = {
    "forge": "forge",
    "neoforge": "neoforge",
    "fabric-loader": "fabric",
    "quilt-loader": "quilt",
}


class PackError(RuntimeError):
    pass


@dataclass
class PackFile:
    path: str
    downloads: list[str]
    sha1: str = ""
    size: int = 0


@dataclass
class Pack:
    name: str
    version: str
    mc_version: str
    loader: str = "vanilla"
    loader_version: str = ""
    files: list[PackFile] = field(default_factory=list)
    summary: str = ""

    @property
    def total_bytes(self) -> int:
        return sum(f.size for f in self.files)


# ---------------------------------------------------------------------------------
# Safety
# ---------------------------------------------------------------------------------

def safe_relative(raw: str) -> PurePosixPath:
    """Validate a path that came out of an archive or an index. Raises if it escapes."""
    text = (raw or "").replace("\\", "/").strip()
    if not text:
        raise PackError("empty path in pack")
    candidate = PurePosixPath(text)
    if candidate.is_absolute() or (len(text) > 1 and text[1] == ":"):
        raise PackError(f"absolute path in pack: {raw!r}")
    if any(part == ".." for part in candidate.parts):
        raise PackError(f"path escapes the instance: {raw!r}")
    return candidate


def resolve_inside(root: Path, relative: PurePosixPath) -> Path:
    """Join and then prove the result is still inside ``root``."""
    target = (root / Path(*relative.parts)).resolve()
    base = root.resolve()
    if base != target and base not in target.parents:
        raise PackError(f"path escapes the instance: {relative}")
    return target


def check_download(url: str) -> str:
    host = urllib.parse.urlsplit(url).hostname or ""
    if host not in ALLOWED_HOSTS:
        raise PackError(
            f"Refusing to download pack content from {host or 'an empty host'}. "
            f"Packs may only fetch from Modrinth and the official loader repositories.")
    return url


# ---------------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------------

def read_index(archive: Path) -> Pack:
    """Parse ``modrinth.index.json`` out of a .mrpack without extracting anything."""
    try:
        with zipfile.ZipFile(ext(Path(archive))) as zf:
            try:
                raw = zf.read(INDEX_NAME)
            except KeyError:
                raise PackError(
                    f"{Path(archive).name} has no {INDEX_NAME}, so it is not a Modrinth "
                    f"modpack. CurseForge packs are a different format.") from None
    except zipfile.BadZipFile as exc:
        raise PackError(f"{Path(archive).name} is not a readable zip: {exc}") from exc

    data = json.loads(raw.decode("utf-8-sig"))
    deps = data.get("dependencies") or {}
    mc_version = deps.get("minecraft") or ""
    if not mc_version:
        raise PackError("The pack does not say which Minecraft version it needs.")

    loader, loader_version = "vanilla", ""
    for key, name in LOADER_KEYS.items():
        if deps.get(key):
            loader, loader_version = name, str(deps[key])
            break

    files: list[PackFile] = []
    for entry in data.get("files") or []:
        # Client-side only: skip anything marked server-only.
        env = entry.get("env") or {}
        if env.get("client") == "unsupported":
            continue
        relative = safe_relative(entry.get("path", ""))
        urls = [u for u in (entry.get("downloads") or []) if isinstance(u, str)]
        if not urls:
            continue
        files.append(PackFile(
            path=str(relative),
            downloads=urls,
            sha1=(entry.get("hashes") or {}).get("sha1", ""),
            size=int(entry.get("fileSize") or 0),
        ))

    return Pack(
        name=str(data.get("name") or Path(archive).stem),
        version=str(data.get("versionId") or ""),
        mc_version=mc_version,
        loader=loader,
        loader_version=loader_version,
        files=files,
        summary=str(data.get("summary") or ""),
    )


# ---------------------------------------------------------------------------------
# Installing
# ---------------------------------------------------------------------------------

def install(layout: Layout, archive: Path, *, name: str | None = None,
            memory_mb: int = 4096, progress: net.Progress | None = None,
            cancel: net.CancelToken = net.NEVER) -> Instance:
    """Turn a .mrpack into a ready-to-play instance."""
    from ..loaders import install_loader

    archive = Path(archive)
    pack = read_index(archive)
    progress = progress or net.Progress()
    log.info("installing pack %s (%s %s %s, %d files)", pack.name, pack.mc_version,
             pack.loader, pack.loader_version, len(pack.files))

    progress.set_phase("Installing loader", f"{pack.loader} {pack.loader_version}")
    version_id = install_loader(layout, pack.loader, pack.mc_version,
                                loader_version=pack.loader_version or None,
                                progress=progress, cancel=cancel)

    store = InstanceStore(layout)
    instance = store.create(name or pack.name, version_id, mc_version=pack.mc_version,
                            loader=pack.loader, loader_version=pack.loader_version,
                            memory_mb=memory_mb)
    root = guard_game_dir(instance.dir)

    jobs: list[net.Job] = []
    for entry in pack.files:
        cancel.check()
        dest = resolve_inside(root, safe_relative(entry.path))
        url = next((check_download(u) for u in entry.downloads
                    if urllib.parse.urlsplit(u).hostname in ALLOWED_HOSTS), None)
        if url is None:
            raise PackError(
                f"{entry.path} can only be fetched from an untrusted host "
                f"({', '.join(urllib.parse.urlsplit(u).hostname or '?' for u in entry.downloads)}).")
        mkdirs(dest.parent)
        jobs.append(net.Job(url, dest, entry.sha1 or None, entry.size or None,
                            label=Path(entry.path).name))

    if jobs:
        net.run_jobs(jobs, progress=progress, phase="Downloading pack", cancel=cancel)

    apply_overrides(archive, root, progress=progress)
    instance.save()
    log.info("pack %s installed as %s", pack.name, instance.slug)
    return instance


def apply_overrides(archive: Path, root: Path, *,
                    progress: net.Progress | None = None) -> int:
    """Copy the pack's ``overrides/`` tree into the instance. Returns the file count."""
    copied = 0
    with zipfile.ZipFile(ext(Path(archive))) as zf:
        members = [i for i in zf.infolist() if not i.is_dir()]
        for info in members:
            parts = PurePosixPath(info.filename.replace("\\", "/")).parts
            if not parts or parts[0] not in OVERRIDE_DIRS or len(parts) < 2:
                continue
            relative = safe_relative("/".join(parts[1:]))
            dest = resolve_inside(root, relative)
            mkdirs(dest.parent)
            with zf.open(info) as src, open(ext(dest), "wb") as out:
                shutil.copyfileobj(src, out)
            copied += 1
            if progress and copied % 25 == 0:
                progress.set_phase("Applying pack files", str(relative))
    if copied:
        log.info("applied %d override files", copied)
    return copied
