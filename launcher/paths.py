r"""Where everything lives on disk.

One rule governs this file: *content-addressed things are shared, mutable things are not.*
``assets/``, ``libraries/``, ``runtimes/`` and ``versions/`` are keyed by hash or by an
immutable id, so every instance may safely point at the same copy. ``instances/<slug>/``
is the game's working directory and is never shared with anything.

Windows MAX_PATH: we keep the data root shallow and additionally route our own file IO
through :func:`ext` (the ``\\?\`` extended-length prefix). That prefix is only ever used
for *our* open()/mkdir() calls -- never for a path handed to java, which cannot parse it.
"""
from __future__ import annotations

import os
import re
import sys
import unicodedata
from pathlib import Path

#: The product name. Also the Azure app registration's display name -- the Minecraft AppID
#: review cross-references the two, so they must not drift apart.
APP_NAME = "Lovely"
APP_VERSION = "1.0.0"

# The launcher package lives at <root>/launcher/paths.py
PACKAGE_DIR = Path(__file__).resolve().parent

#: True when running from a PyInstaller build rather than from source.
FROZEN = bool(getattr(sys, "frozen", False))

if FROZEN:
    # In a one-file build the package is unpacked into a temporary directory that
    # PyInstaller **deletes when the process exits**. Deriving the data root from
    # ``__file__`` there would put instances, worlds and several gigabytes of downloads
    # somewhere that is wiped on close -- the app would silently forget everything, every
    # time. Next to the executable is the only stable answer.
    ROOT = Path(sys.executable).resolve().parent
else:
    ROOT = PACKAGE_DIR.parent


def _default_data_root() -> Path:
    override = os.environ.get("MYFIRE_DATA_ROOT")
    if override:
        return Path(override).expanduser().resolve()
    return ROOT / "data"


class Layout:
    """Resolved directory layout. One instance of this is passed around the core."""

    def __init__(self, data_root: Path | str | None = None) -> None:
        self.data_root = Path(data_root).resolve() if data_root else _default_data_root()

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"Layout({str(self.data_root)!r})"

    # -- shared, content-addressed -------------------------------------------------
    @property
    def assets(self) -> Path:
        return self.data_root / "assets"

    @property
    def asset_objects(self) -> Path:
        return self.assets / "objects"

    @property
    def asset_indexes(self) -> Path:
        return self.assets / "indexes"

    @property
    def asset_virtual(self) -> Path:
        return self.assets / "virtual"

    @property
    def libraries(self) -> Path:
        return self.data_root / "libraries"

    @property
    def versions(self) -> Path:
        return self.data_root / "versions"

    @property
    def runtimes(self) -> Path:
        return self.data_root / "runtimes"

    @property
    def meta_cache(self) -> Path:
        return self.data_root / "meta"

    # -- per-instance, never shared ------------------------------------------------
    @property
    def instances(self) -> Path:
        return self.data_root / "instances"

    def instance_dir(self, slug: str) -> Path:
        return self.instances / slug

    # -- launcher's own state ------------------------------------------------------
    @property
    def settings_file(self) -> Path:
        return self.data_root / "settings.json"

    @property
    def accounts_file(self) -> Path:
        return self.data_root / "accounts.json"

    @property
    def logs(self) -> Path:
        return self.data_root / "logs"

    @property
    def natives_root(self) -> Path:
        return self.data_root / "natives"

    def version_dir(self, version_id: str) -> Path:
        return self.versions / version_id

    def version_json(self, version_id: str) -> Path:
        return self.version_dir(version_id) / f"{version_id}.json"

    def version_jar(self, version_id: str) -> Path:
        return self.version_dir(version_id) / f"{version_id}.jar"

    def ensure(self) -> None:
        for p in (
            self.assets, self.asset_objects, self.asset_indexes, self.libraries,
            self.versions, self.runtimes, self.meta_cache, self.instances,
            self.logs, self.natives_root,
        ):
            mkdirs(p)


DEFAULT = Layout()


# ---------------------------------------------------------------------------------
# Extended-length path handling (Windows MAX_PATH)
# ---------------------------------------------------------------------------------

_EXT_PREFIX = "\\\\?\\"
_UNC_PREFIX = "\\\\?\\UNC" + "\\"


def ext(path: "os.PathLike[str] | str") -> str:
    r"""Return ``path`` prefixed with ``\\?\`` on Windows so it can exceed MAX_PATH.

    Only for our own filesystem calls. Never pass the result to a subprocess: the JVM
    and the Windows command line cannot parse the prefix.
    """
    p = os.fspath(path)
    if sys.platform != "win32":
        return p
    if p.startswith(_EXT_PREFIX):
        return p
    ap = os.path.abspath(p)
    if ap.startswith("\\\\"):  # UNC: \\server\share -> \\?\UNC\server\share
        return _UNC_PREFIX + ap[2:]
    return _EXT_PREFIX + ap


def mkdirs(path: "os.PathLike[str] | str") -> None:
    os.makedirs(ext(path), exist_ok=True)


_SLUG_BAD = re.compile(r"[^a-zA-Z0-9._-]+")
_WIN_RESERVED = {
    "con", "prn", "aux", "nul",
    *(f"com{i}" for i in range(1, 10)),
    *(f"lpt{i}" for i in range(1, 10)),
}


def slugify(name: str) -> str:
    """Filesystem-safe instance folder name. Stable, short, never a reserved device."""
    norm = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode("ascii")
    s = _SLUG_BAD.sub("-", norm).strip("-.")
    s = re.sub(r"-{2,}", "-", s)
    if not s:
        s = "instance"
    if s.lower() in _WIN_RESERVED:
        s = s + "-mc"
    return s[:48]
