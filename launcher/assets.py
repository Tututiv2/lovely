"""Asset index handling.

Assets are content-addressed by SHA-1 and therefore **shared by every instance** -- one
``assets/`` directory for the whole launcher. Instances are the opposite: never shared.
Getting this backwards either wastes gigabytes (per-instance assets) or corrupts worlds
(shared game directories), which is why the brief calls it out twice.

Two legacy layouts exist and the versions that need them are unplayable without:

* ``"virtual": true`` (the 1.6-1.7 era) -- the game wants a real name-shaped tree, so the
  hashed objects are materialised into ``assets/virtual/<index>/`` and that path is passed
  as ``${game_assets}``.
* ``"map_to_resources": true`` (pre-1.6) -- the same tree, but copied into the instance's
  own ``resources/`` folder, because that is where those versions look.

Both are copies rather than links: hard links break when the user edits one, and symlinks
need elevation on Windows.
"""
from __future__ import annotations

import json
import shutil
from dataclasses import dataclass
from pathlib import Path

from . import logs, net
from .paths import Layout, ext, mkdirs

log = logs.get("assets")

RESOURCE_BASE = "https://resources.download.minecraft.net/"


@dataclass
class AssetIndex:
    id: str
    path: Path
    objects: dict
    virtual: bool = False
    map_to_resources: bool = False

    @property
    def total_size(self) -> int:
        return sum(int(o.get("size") or 0) for o in self.objects.values())


def ensure_index(version: dict, layout: Layout, *,
                 cancel: net.CancelToken = net.NEVER) -> AssetIndex | None:
    """Download and cache the asset index named by the version JSON."""
    spec = version.get("assetIndex")
    if not spec:
        # Very old versions name an assets id without an index document.
        legacy_id = version.get("assets")
        if not legacy_id:
            return None
        spec = {"id": legacy_id,
                "url": f"https://launchermeta.mojang.com/v1/packages/"
                       f"{legacy_id}/{legacy_id}.json"}

    index_id = spec["id"]
    dest = layout.asset_indexes / f"{index_id}.json"
    if not net.verify(dest, spec.get("sha1"), spec.get("size")):
        cancel.check()
        mkdirs(dest.parent)
        net.download(spec["url"], dest, sha1=spec.get("sha1"),
                     size=spec.get("size"), cancel=cancel)

    with open(ext(dest), "r", encoding="utf-8") as fh:
        data = json.load(fh)

    return AssetIndex(
        id=index_id,
        path=dest,
        objects=data.get("objects") or {},
        virtual=bool(data.get("virtual")),
        map_to_resources=bool(data.get("map_to_resources")),
    )


def object_path(layout: Layout, hash_: str) -> Path:
    return layout.asset_objects / hash_[:2] / hash_


def plan_downloads(index: AssetIndex, layout: Layout) -> list[net.Job]:
    """One job per asset object that is missing or has the wrong hash.

    There are thousands of these; the caller runs them through a bounded pool. Anything
    already on disk with a matching SHA-1 produces no job at all, which is where the
    "second run downloads zero bytes" guarantee comes from.
    """
    jobs: list[net.Job] = []
    seen: set[str] = set()
    for name, obj in index.objects.items():
        h = obj.get("hash")
        if not h or h in seen:
            continue
        seen.add(h)
        dest = object_path(layout, h)
        size = obj.get("size")
        if net.verify(dest, h, size):
            continue
        jobs.append(net.Job(f"{RESOURCE_BASE}{h[:2]}/{h}", dest, h, size, label=name))
    return jobs


def materialise_legacy(index: AssetIndex, layout: Layout,
                       instance_dir: Path | None = None) -> Path | None:
    """Build the name-shaped tree that pre-1.8 versions require.

    Returns the directory to pass as ``${game_assets}``, or None when the version uses the
    modern hashed layout (which is almost all of them).
    """
    if not (index.virtual or index.map_to_resources):
        return None

    if index.map_to_resources:
        if instance_dir is None:
            return None
        target = Path(instance_dir) / "resources"
    else:
        target = layout.asset_virtual / index.id

    mkdirs(target)
    copied = 0
    for name, obj in index.objects.items():
        h = obj.get("hash")
        if not h:
            continue
        src = object_path(layout, h)
        dst = target.joinpath(*name.split("/"))
        try:
            if dst.exists() and dst.stat().st_size == int(obj.get("size") or 0):
                continue
        except OSError:
            pass
        mkdirs(dst.parent)
        try:
            shutil.copyfile(ext(src), ext(dst))
            copied += 1
        except OSError as exc:
            log.warning("could not materialise legacy asset %s: %s", name, exc)
    if copied:
        log.info("materialised %d legacy assets into %s", copied, target)
    return target
