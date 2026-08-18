r"""Instances -- the isolation that is the whole point of this launcher.

**Every instance gets its own game directory.** Not a shared ``.minecraft``. This is not a
nice-to-have; it is the exact failure the owner already hit:

    Fabric 26.2 with ``fabric-api``, ``voicechat`` and a personal mod live in
    ``%appdata%\.minecraft\mods\``. Install a 1.20.1 Forge mod into the same folder and
    *both* installs break -- Fabric tries to load a Forge jar, Forge tries to load Fabric
    jars, and neither error message mentions the other install.

So ``mods/``, ``saves/``, ``config/``, ``options.txt`` and ``servers.dat`` are per-instance,
while ``assets/``, ``libraries/``, ``runtimes/`` and ``versions/`` are shared -- safely,
because those are content-addressed or keyed by an immutable version id.

:func:`guard_game_dir` is the hard stop: any path that resolves to the real ``.minecraft``
raises rather than launching. It is called on every launch, not just on creation, because a
hand-edited ``instance.json`` is otherwise a loaded gun.
"""
from __future__ import annotations

import json
import os
import shutil
import time
import zipfile
from dataclasses import asdict, dataclass, field
from pathlib import Path

from . import logs, net
from .paths import Layout, ext, mkdirs, slugify

log = logs.get("instances")

INSTANCE_FILE = "instance.json"
SUBDIRS = ("mods", "resourcepacks", "shaderpacks", "saves", "config", "logs",
           "screenshots")

LOADERS = ("vanilla", "fabric", "forge", "neoforge", "quilt")


class InstanceError(RuntimeError):
    pass


def vanilla_minecraft_dir() -> Path | None:
    """The official launcher's ``.minecraft``. We only ever compare against it."""
    appdata = os.environ.get("APPDATA")
    if appdata:
        return Path(appdata) / ".minecraft"
    home = Path.home()
    for candidate in (home / ".minecraft",
                      home / "Library" / "Application Support" / "minecraft"):
        if candidate.exists():
            return candidate
    return None


def guard_game_dir(path: Path) -> Path:
    """Refuse to use the official launcher's game directory as an instance.

    The whole promise of this launcher is that ``%appdata%\\.minecraft\\mods`` is never
    touched. Enforcing it structurally beats remembering to.
    """
    resolved = Path(path).resolve()
    vanilla = vanilla_minecraft_dir()
    if vanilla is not None:
        try:
            v = vanilla.resolve()
        except OSError:
            v = vanilla
        if resolved == v or v in resolved.parents:
            raise InstanceError(
                f"Refusing to use {resolved} as a game directory: that is the official "
                f"launcher's .minecraft. Mixing loaders in one folder is exactly the "
                f"breakage this launcher exists to prevent.")
    return resolved


@dataclass
class Instance:
    """One isolated installation. ``instance.json`` is this, verbatim."""
    slug: str
    name: str
    version_id: str                 # the launchable id, e.g. 1.20.1-forge-47.4.10
    mc_version: str = ""            # the vanilla version underneath it
    loader: str = "vanilla"
    loader_version: str = ""
    memory_mb: int = 4096
    java_override: str = ""
    width: int = 0
    height: int = 0
    extra_jvm_args: list[str] = field(default_factory=list)
    extra_game_args: list[str] = field(default_factory=list)
    quick_play_server: str = ""     # "host:port"; empty means no quick play
    last_played: float = 0.0
    created: float = field(default_factory=time.time)
    notes: str = ""

    # populated on load, not serialised
    _dir: Path | None = field(default=None, repr=False, compare=False)

    # -- persistence ---------------------------------------------------------------
    def to_json(self) -> dict:
        d = asdict(self)
        d.pop("_dir", None)
        return d

    @classmethod
    def from_json(cls, data: dict, directory: Path) -> "Instance":
        known = {f for f in cls.__dataclass_fields__ if not f.startswith("_")}
        inst = cls(**{k: v for k, v in data.items() if k in known})
        inst._dir = directory
        return inst

    @property
    def dir(self) -> Path:
        if self._dir is None:
            raise InstanceError(f"instance {self.slug} has no directory bound")
        return self._dir

    @property
    def mods_dir(self) -> Path:
        return self.dir / "mods"

    @property
    def saves_dir(self) -> Path:
        return self.dir / "saves"

    def save(self) -> None:
        mkdirs(self.dir)
        net.write_atomic(self.dir / INSTANCE_FILE,
                         json.dumps(self.to_json(), indent=2).encode("utf-8"))

    def ensure_dirs(self) -> None:
        guard_game_dir(self.dir)
        for sub in SUBDIRS:
            mkdirs(self.dir / sub)

    def touch_played(self) -> None:
        self.last_played = time.time()
        self.save()

    @property
    def badge(self) -> str:
        if self.loader == "vanilla":
            return self.mc_version or self.version_id
        return f"{self.mc_version or self.version_id} {self.loader.title()}"

    def short_version(self) -> str:
        """A compact label for a 44 px emblem: ``1.20.1`` -> ``1.20``, ``26.2`` -> ``26.2``."""
        v = (self.mc_version or self.version_id or "?").strip()
        parts = v.split(".")
        if len(parts) >= 3 and all(p.isdigit() for p in parts[:2]):
            v = ".".join(parts[:2])
        return v if len(v) <= 6 else v[:6]


# ---------------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------------

class InstanceStore:
    def __init__(self, layout: Layout) -> None:
        self.layout = layout

    def all(self) -> list[Instance]:
        out: list[Instance] = []
        try:
            entries = sorted(self.layout.instances.iterdir())
        except OSError:
            return out
        for d in entries:
            f = d / INSTANCE_FILE
            if not f.is_file():
                continue
            try:
                with open(ext(f), "r", encoding="utf-8-sig") as fh:
                    out.append(Instance.from_json(json.load(fh), d))
            except (OSError, ValueError, TypeError) as exc:
                log.warning("ignoring unreadable instance at %s: %s", d, exc)
        out.sort(key=lambda i: (-i.last_played, i.name.lower()))
        return out

    def get(self, slug: str) -> Instance | None:
        f = self.layout.instance_dir(slug) / INSTANCE_FILE
        try:
            with open(ext(f), "r", encoding="utf-8-sig") as fh:
                return Instance.from_json(json.load(fh), f.parent)
        except (OSError, ValueError, TypeError):
            return None

    def unique_slug(self, name: str) -> str:
        base = slugify(name)
        slug, n = base, 2
        while (self.layout.instance_dir(slug)).exists():
            slug = f"{base}-{n}"
            n += 1
        return slug

    def create(self, name: str, version_id: str, *, mc_version: str = "",
               loader: str = "vanilla", loader_version: str = "",
               memory_mb: int = 4096) -> Instance:
        if not name.strip():
            raise InstanceError("An instance needs a name.")
        slug = self.unique_slug(name)
        inst = Instance(slug=slug, name=name.strip(), version_id=version_id,
                        mc_version=mc_version or version_id, loader=loader,
                        loader_version=loader_version, memory_mb=memory_mb)
        inst._dir = self.layout.instance_dir(slug)
        inst.ensure_dirs()
        inst.save()
        log.info("created instance %s (%s) at %s", inst.name, version_id, inst.dir)
        return inst

    def duplicate(self, inst: Instance, new_name: str) -> Instance:
        slug = self.unique_slug(new_name)
        dest = self.layout.instance_dir(slug)
        guard_game_dir(dest)
        shutil.copytree(ext(inst.dir), ext(dest))
        clone = Instance.from_json(inst.to_json(), dest)
        clone.slug = slug
        clone.name = new_name.strip()
        clone.created = time.time()
        clone.last_played = 0.0
        clone.save()
        return clone

    def rename(self, inst: Instance, new_name: str) -> Instance:
        """Renames the display name only. The folder slug is stable so paths never move."""
        inst.name = new_name.strip()
        inst.save()
        return inst

    def delete(self, inst: Instance, *, delete_saves: bool = False) -> None:
        """Delete an instance. Worlds are preserved unless explicitly sacrificed."""
        guard_game_dir(inst.dir)
        saves = inst.saves_dir
        rescued: Path | None = None
        if not delete_saves and saves.is_dir() and any(saves.iterdir()):
            rescued = self.layout.instances / f"_rescued-saves-{inst.slug}-{int(time.time())}"
            shutil.move(ext(saves), ext(rescued))
            log.warning("kept worlds from %s at %s", inst.name, rescued)
        shutil.rmtree(ext(inst.dir), ignore_errors=True)
        if rescued:
            log.info("worlds preserved at %s", rescued)

    # -- export / import -----------------------------------------------------------
    EXPORT_SKIP = ("logs", "crash-reports", "screenshots")

    def export_zip(self, inst: Instance, dest: Path, *,
                   include_saves: bool = False) -> Path:
        dest = Path(dest)
        mkdirs(dest.parent)
        manifest = {"format": "myfire-instance/1", "instance": inst.to_json(),
                    "include_saves": include_saves, "exported": time.time()}
        with zipfile.ZipFile(ext(dest), "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("manifest.json", json.dumps(manifest, indent=2))
            for root, dirs, files in os.walk(inst.dir):
                rel_root = Path(root).relative_to(inst.dir)
                top = rel_root.parts[0] if rel_root.parts else ""
                if top in self.EXPORT_SKIP or (top == "saves" and not include_saves):
                    dirs[:] = []
                    continue
                for fn in files:
                    if rel_root == Path(".") and fn == INSTANCE_FILE:
                        continue
                    src = Path(root) / fn
                    zf.write(ext(src), str(rel_root / fn).replace("\\", "/"))
        log.info("exported %s -> %s", inst.name, dest)
        return dest

    def import_zip(self, archive: Path, *, name: str | None = None) -> Instance:
        with zipfile.ZipFile(ext(Path(archive))) as zf:
            try:
                manifest = json.loads(zf.read("manifest.json").decode("utf-8"))
            except KeyError as exc:
                raise InstanceError(
                    "That zip has no manifest.json, so it is not an instance export."
                ) from exc
            data = manifest.get("instance") or {}
            display = name or data.get("name") or Path(archive).stem
            slug = self.unique_slug(display)
            dest = self.layout.instance_dir(slug)
            guard_game_dir(dest)
            mkdirs(dest)
            for info in zf.infolist():
                if info.is_dir() or info.filename == "manifest.json":
                    continue
                # Never let an archive path escape the instance directory.
                rel = Path(info.filename.replace("\\", "/"))
                if rel.is_absolute() or ".." in rel.parts:
                    log.warning("skipping unsafe path in export: %s", info.filename)
                    continue
                out = dest / rel
                mkdirs(out.parent)
                with zf.open(info) as src, open(ext(out), "wb") as dst:
                    shutil.copyfileobj(src, dst)
            inst = Instance.from_json(data, dest)
            inst.slug = slug
            inst.name = display
            inst.last_played = 0.0
            inst.ensure_dirs()
            inst.save()
            return inst
