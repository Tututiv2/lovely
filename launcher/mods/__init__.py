"""Mod discovery and installation.

Everything here installs into **one instance's own folder**. There is deliberately no
global mod store and no shared mods directory -- that is the failure the launcher exists
to prevent, so the API takes an :class:`~launcher.instances.Instance` and never a bare path.
"""
from .modrinth import (  # noqa: F401
    InstallResult, ModFile, ModProject, ModVersion, ModrinthError,
    best_version, install, installed_filenames, search, versions,
)

__all__ = [
    "ModProject", "ModVersion", "ModFile", "InstallResult", "ModrinthError",
    "search", "versions", "best_version", "install", "installed_filenames",
]
