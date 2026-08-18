"""Java runtimes -- downloaded and managed by the launcher, never by the user.

Requiring the right JDK by hand is the single biggest source of "it won't start", and on
this machine it is guaranteed: the only system-wide Java is 25, which nothing below 1.20.5
will run on. So the launcher fetches its own.

The version JSON says what it needs::

    "javaVersion": { "component": "java-runtime-gamma", "majorVersion": 17 }

Versions before 1.17 have **no such field**, and the correct default for them is Java 8 --
not "whatever is installed". A launcher that quietly runs 1.12.2 on Java 21 produces a wall
of ``Unsupported class file major version`` and has failed.

Two sources, in order:

1. **Mojang's own runtime index** (``piston-meta``), which is per-platform and per-component
   and gives a file manifest with a SHA-1 for every file.
2. **Adoptium** as a fallback, for the components Mojang does not publish for a platform
   (notably: there is no ``jre-legacy`` for Windows on ARM).

Runtimes live in ``runtimes/<component>/`` and are shared by every instance.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import stat
import subprocess
import sys
import zipfile
from dataclasses import dataclass
from pathlib import Path

from . import logs, net, rules
from .paths import Layout, ext, mkdirs

log = logs.get("java")

# NOTE: the index is *not* at .../java-runtime/all.json -- that path 404s with an Azure
# blob error. The real document sits behind a content hash segment. Verified live.
RUNTIME_INDEX_URL = ("https://piston-meta.mojang.com/v1/products/java-runtime/"
                     "2ec0cc96c44e5a76b9c8b7c39df7210883d12871/all.json")
ADOPTIUM_URL = ("https://api.adoptium.net/v3/binary/latest/{major}/ga/{os}/{arch}"
                "/jre/hotspot/normal/eclipse")

#: Mojang's component names, and the Java major each one actually is.
COMPONENT_MAJOR = {
    "jre-legacy": 8,
    "java-runtime-alpha": 16,
    "java-runtime-beta": 17,
    "java-runtime-gamma": 17,
    "java-runtime-gamma-snapshot": 17,
    "java-runtime-delta": 21,
    "java-runtime-epsilon": 25,   # what 26.x asks for
}
#: The reverse, for picking a component when we only know the major.
MAJOR_COMPONENT = {8: "jre-legacy", 16: "java-runtime-alpha", 17: "java-runtime-gamma",
                   21: "java-runtime-delta", 25: "java-runtime-epsilon"}


class JavaError(RuntimeError):
    pass


@dataclass(frozen=True)
class JavaRequirement:
    component: str
    major: int

    def __str__(self) -> str:
        return f"Java {self.major} ({self.component})"


def requirement_for(version: dict) -> JavaRequirement:
    """What Java this version needs. Pre-1.17 versions say nothing and mean 8."""
    spec = version.get("javaVersion") or {}
    major = spec.get("majorVersion")
    component = spec.get("component")
    if major is None and component is None:
        return JavaRequirement("jre-legacy", 8)
    if major is None:
        major = COMPONENT_MAJOR.get(component, 17)
    if not component:
        component = MAJOR_COMPONENT.get(int(major), f"java-runtime-{major}")
    return JavaRequirement(component, int(major))


def platform_key() -> str:
    """Mojang's key into the runtime index for this machine."""
    o = rules.current_os()
    if o.name == "windows":
        return {"x64": "windows-x64", "x86": "windows-x86",
                "arm64": "windows-arm64"}.get(o.arch, "windows-x64")
    if o.name == "osx":
        return "mac-os-arm64" if o.arch == "arm64" else "mac-os"
    return {"x64": "linux", "x86": "linux-i386"}.get(o.arch, "linux")


def adoptium_platform() -> tuple[str, str]:
    o = rules.current_os()
    os_name = {"windows": "windows", "osx": "mac", "linux": "linux"}[o.name]
    arch = {"x64": "x64", "x86": "x86", "arm64": "aarch64", "arm32": "arm"}.get(
        o.arch, "x64")
    return os_name, arch


def java_executable(home: Path) -> Path:
    name = "java.exe" if sys.platform == "win32" else "java"
    direct = home / "bin" / name
    if os.path.exists(ext(direct)):
        return direct
    # macOS bundles nest it under jre.bundle/...; Adoptium archives nest one level under
    # a versioned folder. Both are shallow, so a bounded walk is enough.
    try:
        for candidate in sorted(home.glob(f"*/bin/{name}")) + \
                sorted(home.glob(f"*/*/bin/{name}")) + \
                sorted(home.glob(f"*/*/*/bin/{name}")):
            return candidate
    except OSError:
        pass
    raise JavaError(f"no java executable under {home}")


def installed_home(layout: Layout, component: str) -> Path:
    return layout.runtimes / component


def is_installed(layout: Layout, component: str) -> bool:
    try:
        return java_executable(installed_home(layout, component)).is_file()
    except (JavaError, OSError):
        return False


# ---------------------------------------------------------------------------------
# Probing an existing JVM
# ---------------------------------------------------------------------------------

_VERSION_RE = re.compile(r'version "?(\d+)(?:\.(\d+))?')


def probe_major(java_path: Path | str) -> int | None:
    """Ask a java binary what major version it is. Returns None if it will not answer."""
    try:
        proc = subprocess.run(
            [str(java_path), "-version"], capture_output=True, text=True, timeout=20,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except (OSError, subprocess.SubprocessError):
        return None
    blob = (proc.stderr or "") + (proc.stdout or "")
    m = _VERSION_RE.search(blob)
    if not m:
        return None
    major, minor = int(m.group(1)), int(m.group(2) or 0)
    return minor if major == 1 else major  # "1.8.0_452" is Java 8


# ---------------------------------------------------------------------------------
# Mojang runtime index
# ---------------------------------------------------------------------------------

def _runtime_index(layout: Layout, *, refresh: bool = False) -> dict:
    cache = layout.meta_cache / "java-runtime-all.json"
    if not refresh:
        try:
            with open(ext(cache), "r", encoding="utf-8") as fh:
                return json.load(fh)
        except (OSError, ValueError):
            pass
    raw = net.get_bytes(RUNTIME_INDEX_URL)
    mkdirs(cache.parent)
    net.write_atomic(cache, raw)
    return json.loads(raw.decode("utf-8"))


def _install_from_mojang(layout: Layout, component: str, *,
                         progress: net.Progress | None = None,
                         cancel: net.CancelToken = net.NEVER) -> Path:
    index = _runtime_index(layout)
    plat = platform_key()
    entries = (index.get(plat) or {}).get(component) or []
    if not entries:
        raise JavaError(f"Mojang publishes no {component} for {plat}")

    manifest_spec = entries[0].get("manifest") or {}
    if not manifest_spec.get("url"):
        raise JavaError(f"malformed runtime entry for {component}/{plat}")

    if progress:
        progress.set_phase("Downloading Java", f"{component} for {plat}")
    manifest = json.loads(net.get_bytes(manifest_spec["url"]).decode("utf-8"))

    home = installed_home(layout, component)
    mkdirs(home)

    files: dict = manifest.get("files") or {}
    jobs: list[net.Job] = []
    links: list[tuple[Path, str]] = []
    executables: list[Path] = []

    for rel, spec in files.items():
        dest = home.joinpath(*rel.split("/"))
        kind = spec.get("type")
        if kind == "directory":
            mkdirs(dest)
        elif kind == "link":
            links.append((dest, spec.get("target", "")))
        elif kind == "file":
            raw = (spec.get("downloads") or {}).get("raw") or {}
            if not raw.get("url"):
                continue
            jobs.append(net.Job(raw["url"], dest, raw.get("sha1"), raw.get("size"),
                                label=rel))
            if spec.get("executable"):
                executables.append(dest)

    net.run_jobs(jobs, concurrency=12, progress=progress,
                 phase=f"Downloading Java {COMPONENT_MAJOR.get(component, '')}".strip(),
                 cancel=cancel)

    for dest, target in links:
        try:
            mkdirs(dest.parent)
            src = (dest.parent / target).resolve()
            if os.path.lexists(ext(dest)):
                continue
            if sys.platform == "win32":
                # Symlinks need elevation or developer mode on Windows; a copy is
                # equivalent for a JRE tree and always works.
                if src.is_dir():
                    shutil.copytree(ext(src), ext(dest), dirs_exist_ok=True)
                elif src.exists():
                    shutil.copyfile(ext(src), ext(dest))
            else:
                os.symlink(target, dest)
        except OSError as exc:
            log.debug("runtime link %s -> %s failed: %s", dest, target, exc)

    if sys.platform != "win32":
        for exe in executables:
            try:
                st = os.stat(exe)
                os.chmod(exe, st.st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
            except OSError:
                pass

    return home


def _install_from_adoptium(layout: Layout, component: str, major: int, *,
                           progress: net.Progress | None = None,
                           cancel: net.CancelToken = net.NEVER) -> Path:
    os_name, arch = adoptium_platform()
    url = ADOPTIUM_URL.format(major=major, os=os_name, arch=arch)
    if progress:
        progress.set_phase("Downloading Java", f"Adoptium {major} ({os_name}/{arch})")
    log.info("falling back to Adoptium for Java %s", major)

    archive = layout.meta_cache / f"adoptium-{major}-{os_name}-{arch}"
    archive = archive.with_suffix(".zip" if os_name == "windows" else ".tar.gz")
    net.download(url, archive, cancel=cancel,
                 on_bytes=progress.add_bytes if progress else None)

    home = installed_home(layout, component)
    mkdirs(home)
    if archive.suffix == ".zip":
        with zipfile.ZipFile(ext(archive)) as zf:
            zf.extractall(ext(home))
    else:
        import tarfile
        with tarfile.open(ext(archive)) as tf:
            tf.extractall(ext(home))
    try:
        os.unlink(ext(archive))
    except OSError:
        pass
    return home


def ensure_runtime(layout: Layout, req: JavaRequirement, *,
                   progress: net.Progress | None = None,
                   cancel: net.CancelToken = net.NEVER) -> Path:
    """Return a path to a ``java`` binary satisfying ``req``, downloading it if needed."""
    home = installed_home(layout, req.component)
    if is_installed(layout, req.component):
        exe = java_executable(home)
        found = probe_major(exe)
        if found is None or found == req.major:
            return exe
        log.warning("installed %s reports Java %s, expected %s -- reinstalling",
                    req.component, found, req.major)
        shutil.rmtree(ext(home), ignore_errors=True)

    try:
        _install_from_mojang(layout, req.component, progress=progress, cancel=cancel)
    except (JavaError, net.NetError) as exc:
        log.info("Mojang runtime unavailable (%s); trying Adoptium", exc)
        _install_from_adoptium(layout, req.component, req.major,
                               progress=progress, cancel=cancel)

    exe = java_executable(installed_home(layout, req.component))
    found = probe_major(exe)
    if found is not None and found != req.major:
        raise JavaError(
            f"Installed runtime for {req} reports Java {found}. Refusing to launch with "
            "the wrong Java -- that produces 'Unsupported class file major version'.")
    log.info("using %s at %s", req, exe)
    return exe


def resolve_java(layout: Layout, version: dict, *, override: str | None = None,
                 progress: net.Progress | None = None,
                 cancel: net.CancelToken = net.NEVER) -> Path:
    """The Java this launch will use: an explicit override if given, else the managed one."""
    req = requirement_for(version)
    if override:
        p = Path(override)
        if p.is_dir():
            p = java_executable(p)
        if not p.is_file():
            raise JavaError(f"Java override {override!r} does not exist.")
        found = probe_major(p)
        if found is not None and found != req.major:
            log.warning("Java override is %s but %s wants %s; launching anyway because "
                        "it was set explicitly", found, version.get("id"), req.major)
        return p
    return ensure_runtime(layout, req, progress=progress, cancel=cancel)
