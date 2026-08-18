"""Library selection, the classpath, and natives.

Two things here are easy to get subtly wrong.

**Natives are packaged two different ways and both are current.**

* *Old style* (roughly <= 1.18): the library carries ``natives: {"windows": "natives-windows"}``
  naming a key into ``downloads.classifiers``. That jar is downloaded and **extracted** into
  a natives directory, honouring ``extract.exclude`` (nearly always ``["META-INF/"]``).
* *New style* (1.19+): natives are ordinary libraries whose Maven coordinate carries the
  classifier, e.g. ``org.lwjgl:lwjgl:3.3.1:natives-windows``. There is no ``classifiers``
  block and nothing to extract; LWJGL unpacks itself at runtime. A version with no
  ``classifiers`` is not broken -- it is the new format.

**Where a library's bytes come from varies.** Vanilla libraries carry a full
``downloads.artifact`` with url, size and sha1. Fabric and Forge libraries often carry only
a ``url`` naming a Maven *repository root*, to which the coordinate's path is appended. And
libraries the Forge installer produced locally carry neither, because they already exist on
disk -- those are checked for presence, not downloaded.

Natives are extracted into a **fresh directory per launch**, deleted when the process ends.
Sharing one natives directory between two running instances is a file-lock failure on
Windows, and it presents as a crash with no useful message.
"""
from __future__ import annotations

import os
import shutil
import sys
import time
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

from . import logs, net, rules
from .paths import Layout, ext, mkdirs
from .rules import Features, OsInfo
from .versions import Maven, VersionError

log = logs.get("libraries")

MAVEN_CENTRAL = "https://libraries.minecraft.net/"


@dataclass
class LibraryFile:
    """One jar we need: where it goes, where it comes from, and what it is for."""
    name: str
    path: Path
    url: str | None
    sha1: str | None
    size: int | None
    is_native: bool = False
    extract_exclude: tuple[str, ...] = ()

    @property
    def present(self) -> bool:
        return os.path.exists(ext(self.path))


def _artifact_dest(layout: Layout, rel_path: str) -> Path:
    return layout.libraries.joinpath(*rel_path.split("/"))


def _native_key(lib: dict, os_info: OsInfo) -> str | None:
    natives = lib.get("natives")
    if not natives:
        return None
    key = natives.get(os_info.natives_key)
    if not key:
        return None
    # "${arch}" appears in a handful of old entries: natives-windows-${arch}
    return key.replace("${arch}", "64" if os_info.arch in ("x64", "arm64") else "32")


def iter_library_files(version: dict, layout: Layout, *,
                       os_info: OsInfo | None = None,
                       features: Features | None = None) -> Iterator[LibraryFile]:
    """Yield every library file this version needs on this machine, rules applied."""
    os_info = os_info or rules.current_os()
    features = features or Features()

    for lib in version.get("libraries") or []:
        if not rules.allowed(lib.get("rules"), os_info=os_info, features=features):
            continue
        name = lib.get("name") or "<unnamed>"
        downloads = lib.get("downloads") or {}

        # --- main artifact -------------------------------------------------------
        artifact = downloads.get("artifact")
        if artifact and artifact.get("path"):
            yield LibraryFile(
                name=name,
                path=_artifact_dest(layout, artifact["path"]),
                url=artifact.get("url") or None,
                sha1=artifact.get("sha1"),
                size=artifact.get("size"),
            )
        elif lib.get("name") and "classifiers" not in downloads:
            # No artifact block. Derive the path from the coordinate and, if the library
            # names a repository root, the URL too. Forge-installed libraries name no
            # repository at all and simply have to be there already.
            try:
                coord = Maven.parse(name)
            except VersionError:
                log.debug("skipping unparseable library name %r", name)
                continue
            rel = coord.path()
            base = lib.get("url")
            url = None
            if base:
                url = base.rstrip("/") + "/" + rel
            elif not lib.get("natives"):
                url = MAVEN_CENTRAL + rel
            yield LibraryFile(name=name, path=_artifact_dest(layout, rel), url=url,
                              sha1=(lib.get("checksums") or [None])[0], size=None)

        # --- old-style natives ---------------------------------------------------
        key = _native_key(lib, os_info)
        if key:
            classifier = (downloads.get("classifiers") or {}).get(key)
            if classifier and classifier.get("path"):
                excl = tuple((lib.get("extract") or {}).get("exclude") or ("META-INF/",))
                yield LibraryFile(
                    name=f"{name}:{key}",
                    path=_artifact_dest(layout, classifier["path"]),
                    url=classifier.get("url") or None,
                    sha1=classifier.get("sha1"),
                    size=classifier.get("size"),
                    is_native=True,
                    extract_exclude=excl,
                )
            elif not downloads:
                # Old-format library with natives but no downloads block at all: derive.
                try:
                    coord = Maven.parse(name)
                except VersionError:
                    continue
                native_coord = Maven(coord.group, coord.artifact, coord.version, key)
                rel = native_coord.path()
                base = lib.get("url") or MAVEN_CENTRAL
                excl = tuple((lib.get("extract") or {}).get("exclude") or ("META-INF/",))
                yield LibraryFile(
                    name=f"{name}:{key}",
                    path=_artifact_dest(layout, rel),
                    url=base.rstrip("/") + "/" + rel,
                    sha1=None, size=None, is_native=True, extract_exclude=excl,
                )


def client_jar_download(version: dict) -> dict | None:
    return ((version.get("downloads") or {}).get("client")) or None


def client_jar_dest(version: dict, layout: Layout) -> Path:
    """Where the *vanilla* client jar is downloaded to.

    A modded version's ``downloads.client`` is inherited from the vanilla root, so it is
    fetched once under the root id. Otherwise every Forge, Fabric and NeoForge instance of
    1.20.1 pulls its own byte-identical 21 MB copy and ``versions/`` stops being shared.
    """
    from .versions import ROOT_ID_KEY
    return layout.version_jar(version.get(ROOT_ID_KEY) or version["id"])


def materialise_client_jar(version: dict, layout: Layout) -> Path:
    r"""Make sure the client jar also exists under the *launched* version's own id.

    Downloading once and pointing the classpath at the shared copy is not enough, and the
    reason is specific: modern Forge runs on the module path and passes

        -DignoreList=...,forge-,${version_name}.jar

    telling BootstrapLauncher to keep that **filename** out of the module graph. Point the
    classpath at ``1.20.1.jar`` instead of ``1.20.1-forge-47.4.10.jar`` and the entry no
    longer matches, so the client jar becomes an automatic module and the launch dies with

        ResolutionException: Modules _1._20._1 and minecraft export package
        net.minecraft.client to module MixinExtras

    So the file has to be named after the version being launched. A hard link gives that
    for free on NTFS and on any sane POSIX filesystem; a copy is the fallback.
    """
    from .versions import ROOT_ID_KEY
    root_id = version.get(ROOT_ID_KEY) or version["id"]
    own = layout.version_jar(version["id"])
    if root_id == version["id"] or os.path.exists(ext(own)):
        return own if os.path.exists(ext(own)) else layout.version_jar(root_id)

    source = layout.version_jar(root_id)
    if not os.path.exists(ext(source)):
        return source  # nothing to link yet; the caller's download plan will have failed

    mkdirs(own.parent)
    try:
        os.link(ext(source), ext(own))
        log.debug("hard-linked client jar %s -> %s", source.name, own.name)
    except OSError:
        shutil.copyfile(ext(source), ext(own))
        log.debug("copied client jar %s -> %s (hard link unavailable)",
                  source.name, own.name)
    return own


def plan_downloads(version: dict, layout: Layout, *,
                   os_info: OsInfo | None = None,
                   features: Features | None = None) -> tuple[list[net.Job], list[LibraryFile]]:
    """Return (download jobs, all library files) for libraries + the client jar."""
    files = list(iter_library_files(version, layout, os_info=os_info, features=features))
    jobs: list[net.Job] = []
    for f in files:
        if f.url and not net.verify(f.path, f.sha1, f.size):
            jobs.append(net.Job(f.url, f.path, f.sha1, f.size, label=f.name))

    client = client_jar_download(version)
    if client and client.get("url"):
        dest = client_jar_dest(version, layout)
        if not net.verify(dest, client.get("sha1"), client.get("size")):
            jobs.append(net.Job(client["url"], dest, client.get("sha1"),
                                client.get("size"), label=dest.name))
    return jobs, files


def missing_local(files: list[LibraryFile]) -> list[LibraryFile]:
    """Libraries that have no URL and are not on disk -- an incomplete loader install."""
    return [f for f in files if not f.url and not f.present]


def build_classpath(version: dict, layout: Layout, files: list[LibraryFile], *,
                    os_info: OsInfo | None = None) -> list[str]:
    """Classpath entries in order: libraries as merged, then the client jar last.

    Order matters and is inherited straight from the merged ``libraries`` list, which is
    why the merge in :mod:`launcher.versions` puts the child's entries first.
    """
    os_info = os_info or rules.current_os()
    entries: list[str] = []
    seen: set[str] = set()
    for f in files:
        if f.is_native:
            continue  # old-style natives are extracted, never on the classpath
        p = str(f.path)
        if p in seen:
            continue
        seen.add(p)
        entries.append(p)

    # Prefer a jar the version owns -- some loader installers emit a *patched* client jar
    # under the modded id, and that one must win. Otherwise use the shared vanilla jar.
    own = layout.version_jar(version["id"])
    client = own if os.path.exists(ext(own)) else client_jar_dest(version, layout)
    entries.append(str(client))
    return entries


def join_classpath(entries: list[str], os_info: OsInfo | None = None) -> str:
    os_info = os_info or rules.current_os()
    return os_info.classpath_separator.join(entries)


# ---------------------------------------------------------------------------------
# Natives
# ---------------------------------------------------------------------------------

def extract_natives(files: list[LibraryFile], target: Path) -> int:
    """Extract old-style natives jars into ``target``. Returns the file count.

    Zero is a normal, healthy answer on 1.19+ -- those versions have no extractable
    natives at all. The directory is still created and still passed as
    ``java.library.path``, because older code paths read it unconditionally.
    """
    mkdirs(target)
    count = 0
    for f in files:
        if not f.is_native or not f.present:
            continue
        try:
            with zipfile.ZipFile(ext(f.path)) as zf:
                for info in zf.infolist():
                    nm = info.filename
                    if info.is_dir() or nm.endswith("/"):
                        continue
                    if any(nm.startswith(x) for x in f.extract_exclude):
                        continue
                    # Flatten: natives jars nest by directory but the loader wants them
                    # side by side, and a nested path can also escape the target.
                    out = target / Path(nm).name
                    with zf.open(info) as src, open(ext(out), "wb") as dst:
                        shutil.copyfileobj(src, dst)
                    count += 1
        except (OSError, zipfile.BadZipFile) as exc:
            raise RuntimeError(f"corrupt natives jar {f.path}: {exc}") from exc
    log.debug("extracted %d native files to %s", count, target)
    return count


OWNER_FILE = ".owner-pid"


def claim_natives(target: Path, pid: int) -> None:
    """Record which process owns a natives directory, so a sweep can tell if it is dead."""
    try:
        (target / OWNER_FILE).write_text(str(pid), encoding="ascii")
    except OSError:
        log.debug("could not claim natives dir %s", target, exc_info=True)


def cleanup_natives(target: Path) -> None:
    try:
        shutil.rmtree(ext(target), ignore_errors=True)
    except Exception:  # pragma: no cover - best effort
        log.debug("could not remove natives dir %s", target, exc_info=True)


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if sys.platform == "win32":
        import ctypes
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        handle = ctypes.windll.kernel32.OpenProcess(
            PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return False
        code = ctypes.c_ulong()
        ok = ctypes.windll.kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
        ctypes.windll.kernel32.CloseHandle(handle)
        return bool(ok) and code.value == 259  # STILL_ACTIVE
    try:
        os.kill(pid, 0)
        return True
    except (OSError, ProcessLookupError):
        return False


def sweep_stale_natives(root: Path, *, max_age_hours: float = 12.0) -> int:
    """Delete natives directories whose game is gone. Returns how many were removed.

    The per-launch directory is normally removed by the reaper thread, but that thread dies
    with the launcher -- and the launcher is explicitly allowed to be closed while the game
    runs. So directories do leak, and they are swept on the next start instead: a directory
    goes only when its owning process is gone, or when it is old enough that no unclaimed
    launch could still be using it.
    """
    removed = 0
    now = time.time()
    try:
        entries = list(Path(root).iterdir())
    except OSError:
        return 0
    for d in entries:
        if not d.is_dir():
            continue
        owner = d / OWNER_FILE
        try:
            if owner.is_file():
                if _pid_alive(int(owner.read_text(encoding="ascii").strip() or 0)):
                    continue
            elif now - d.stat().st_mtime < max_age_hours * 3600:
                continue  # unclaimed and recent: a launch may still be assembling it
        except (OSError, ValueError):
            pass
        cleanup_natives(d)
        removed += 1
    if removed:
        log.info("swept %d stale natives director%s", removed,
                 "y" if removed == 1 else "ies")
    return removed
