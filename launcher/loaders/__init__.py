"""Mod loader installation, behind one dispatch function.

Every loader ends at the same place: a version JSON in ``versions/<id>/<id>.json`` that
declares ``inheritsFrom``. From there :func:`launcher.versions.resolve` treats a modded
version exactly like a vanilla one, which is why nothing downstream of this package knows
what a mod loader is.
"""
from __future__ import annotations

from .. import net
from ..paths import Layout
from . import fabric as _fabric
from . import forge as _forge

LOADERS = ("vanilla", "fabric", "quilt", "forge", "neoforge")


def loader_versions(loader: str, mc_version: str) -> list[str]:
    """Loader builds compatible with ``mc_version``, newest first. Empty means none."""
    loader = loader.lower()
    if loader in ("fabric", "quilt"):
        try:
            return _fabric.loader_versions(mc_version, _fabric.FLAVOURS[loader])
        except net.NetError:
            return []
    if loader == "forge":
        try:
            return _forge.forge_versions(mc_version)
        except net.NetError:
            return []
    if loader == "neoforge":
        return _forge.neoforge_versions(mc_version)
    return []


def install_loader(layout: Layout, loader: str, mc_version: str, *,
                   loader_version: str | None = None,
                   progress: net.Progress | None = None,
                   cancel: net.CancelToken = net.NEVER) -> str:
    """Install ``loader`` for ``mc_version`` and return the launchable version id."""
    loader = (loader or "vanilla").lower()
    if loader == "vanilla":
        return mc_version
    if loader in ("fabric", "quilt"):
        return _fabric.install(layout, mc_version, loader_version=loader_version,
                               flavour=_fabric.FLAVOURS[loader], progress=progress,
                               cancel=cancel)
    if loader == "forge":
        return _forge.install(layout, mc_version, loader_version=loader_version,
                              flavour=_forge.FORGE, progress=progress, cancel=cancel)
    if loader == "neoforge":
        return _forge.install(layout, mc_version, loader_version=loader_version,
                              flavour=_forge.NEOFORGE, progress=progress, cancel=cancel)
    raise ValueError(f"unknown loader {loader!r}; expected one of {LOADERS}")


__all__ = ["LOADERS", "install_loader", "loader_versions"]
