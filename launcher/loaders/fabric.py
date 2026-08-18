"""Fabric and Quilt -- one API call each, and the biggest payoff per line in the project.

Both projects publish a *ready-made version JSON* through their meta API. There is no
installer to run, no processor pipeline, no patched jars: fetch the profile, write it to
``versions/<id>/<id>.json``, and the existing ``inheritsFrom`` resolver does the rest. The
only real work is picking a loader version compatible with the chosen Minecraft version.

Quilt is Fabric's API shape at a different host, so both share every function here.
"""
from __future__ import annotations

import json
from dataclasses import dataclass

from .. import logs, net
from ..paths import Layout, mkdirs

log = logs.get("loaders.fabric")

FABRIC_META = "https://meta.fabricmc.net/v2"
QUILT_META = "https://meta.quiltmc.org/v3"


@dataclass(frozen=True)
class Flavour:
    key: str
    meta: str
    label: str


FABRIC = Flavour("fabric", FABRIC_META, "Fabric")
QUILT = Flavour("quilt", QUILT_META, "Quilt")
FLAVOURS = {"fabric": FABRIC, "quilt": QUILT}


def game_versions(flavour: Flavour = FABRIC, *, stable_only: bool = True) -> list[str]:
    """Minecraft versions this loader supports."""
    data = net.get_json(f"{flavour.meta}/versions/game")
    return [v["version"] for v in data if v.get("stable") or not stable_only]


def loader_versions(mc_version: str, flavour: Flavour = FABRIC, *,
                    stable_only: bool = False) -> list[str]:
    """Loader versions compatible with ``mc_version``, newest first.

    The endpoint is already filtered by Minecraft version, which is what lets the create
    dialog offer only versions that can actually work with the chosen game version.
    """
    data = net.get_json(f"{flavour.meta}/versions/loader/{mc_version}")
    out = []
    for entry in data:
        loader = entry.get("loader") or {}
        if stable_only and not loader.get("stable", False):
            continue
        if loader.get("version"):
            out.append(loader["version"])
    return out


def latest_loader(mc_version: str, flavour: Flavour = FABRIC) -> str:
    versions = loader_versions(mc_version, flavour, stable_only=True) \
        or loader_versions(mc_version, flavour)
    if not versions:
        raise net.NetError(
            f"{flavour.label} publishes no loader for Minecraft {mc_version}.")
    return versions[0]


def install(layout: Layout, mc_version: str, *, loader_version: str | None = None,
            flavour: Flavour = FABRIC,
            progress: net.Progress | None = None,
            cancel: net.CancelToken = net.NEVER) -> str:
    """Install a loader profile and return the launchable version id."""
    loader_version = loader_version or latest_loader(mc_version, flavour)
    if progress:
        progress.set_phase(f"Installing {flavour.label}",
                           f"{mc_version} loader {loader_version}")

    url = (f"{flavour.meta}/versions/loader/{mc_version}/{loader_version}"
           f"/profile/json")
    raw = net.get_bytes(url, cancel=cancel)
    profile = json.loads(raw.decode("utf-8"))

    version_id = profile.get("id")
    if not version_id:
        raise net.NetError(f"{flavour.label} profile for {mc_version} has no id")
    if not profile.get("inheritsFrom"):
        # Every published profile inherits; if one ever stops, resolution would silently
        # produce a version with no assets rather than failing.
        profile["inheritsFrom"] = mc_version
        raw = json.dumps(profile, indent=2).encode("utf-8")

    dest = layout.version_json(version_id)
    mkdirs(dest.parent)
    net.write_atomic(dest, raw)
    log.info("installed %s profile %s", flavour.label, version_id)
    return version_id
