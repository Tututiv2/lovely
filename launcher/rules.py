r"""Rule evaluation -- the gate on every library and every argument.

Mojang attaches a ``rules`` list to libraries and to individual command-line arguments.
The semantics are small but every one of them matters:

* Evaluate top to bottom; **the last rule that matches wins.**
* If a ``rules`` list exists and *nothing* matched, the default is **deny**.
* If there is no ``rules`` list at all, allow.
* A rule with no ``os`` and no ``features`` block matches unconditionally.
* ``os.version`` is a **regular expression**, not a literal. ``^10\.`` is the common one
  and a literal comparison silently drops LWJGL on Windows.

``features`` gate arguments rather than libraries: demo mode, custom resolution, and the
Quick Play family.
"""
from __future__ import annotations

import platform
import re
import sys
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any, Mapping, Sequence


@dataclass(frozen=True)
class OsInfo:
    name: str      # windows | osx | linux
    arch: str      # x86 | x64 | arm64 | arm32
    version: str   # raw OS version string, matched against a regex

    @property
    def is_windows(self) -> bool:
        return self.name == "windows"

    @property
    def is_osx(self) -> bool:
        return self.name == "osx"

    @property
    def classpath_separator(self) -> str:
        return ";" if self.is_windows else ":"

    @property
    def natives_key(self) -> str:
        """The key used in old-style ``natives: {...}`` blocks."""
        return {"windows": "windows", "osx": "osx", "linux": "linux"}[self.name]


def _norm_arch(machine: str, bits: int) -> str:
    m = machine.lower()
    if m in ("amd64", "x86_64", "x64"):
        return "x64"
    if m in ("arm64", "aarch64"):
        return "arm64"
    if m.startswith("arm"):
        return "arm32"
    if m in ("i386", "i686", "x86"):
        return "x86"
    return "x64" if bits == 64 else "x86"


@lru_cache(maxsize=1)
def current_os() -> OsInfo:
    if sys.platform.startswith("win"):
        name = "windows"
        version = platform.version() or platform.release()
    elif sys.platform == "darwin":
        name = "osx"
        version = platform.mac_ver()[0] or platform.release()
    else:
        name = "linux"
        version = platform.release()
    bits = 64 if sys.maxsize > 2 ** 32 else 32
    return OsInfo(name, _norm_arch(platform.machine(), bits), version)


@dataclass
class Features:
    """Launch-time feature flags, set from real UI state -- never hardcoded true."""
    is_demo_user: bool = False
    has_custom_resolution: bool = False
    has_quick_plays_support: bool = False
    is_quick_play_singleplayer: bool = False
    is_quick_play_multiplayer: bool = False
    is_quick_play_realms: bool = False
    extra: dict[str, bool] = field(default_factory=dict)

    def as_dict(self) -> dict[str, bool]:
        d = {
            "is_demo_user": self.is_demo_user,
            "has_custom_resolution": self.has_custom_resolution,
            "has_quick_plays_support": self.has_quick_plays_support,
            "is_quick_play_singleplayer": self.is_quick_play_singleplayer,
            "is_quick_play_multiplayer": self.is_quick_play_multiplayer,
            "is_quick_play_realms": self.is_quick_play_realms,
        }
        d.update(self.extra)
        return d

    def get(self, key: str) -> bool:
        return bool(self.as_dict().get(key, False))


def _os_matches(spec: Mapping[str, Any], os_info: OsInfo) -> bool:
    want_name = spec.get("name")
    if want_name is not None and want_name != os_info.name:
        return False
    want_arch = spec.get("arch")
    if want_arch is not None:
        # Mojang writes "x86" for the 32-bit rules; treat the field as an exact match on
        # our normalised value, with the one historical alias folded in.
        alias = {"x86_64": "x64", "amd64": "x64", "aarch64": "arm64"}
        if alias.get(want_arch, want_arch) != os_info.arch:
            return False
    want_version = spec.get("version")
    if want_version is not None:
        try:
            if re.search(want_version, os_info.version) is None:
                return False
        except re.error:
            return want_version == os_info.version
    return True


def _features_match(spec: Mapping[str, Any], features: Features) -> bool:
    # Every key in the block must equal the launcher's current state. A rule asking for
    # {"is_demo_user": false} matches only when demo mode is off.
    return all(bool(features.get(k)) == bool(v) for k, v in spec.items())


def allowed(rules: Sequence[Mapping[str, Any]] | None, *,
            os_info: OsInfo | None = None,
            features: Features | None = None) -> bool:
    """Apply the rule list. See the module docstring for the exact semantics."""
    if not rules:
        return True
    os_info = os_info or current_os()
    features = features or Features()

    verdict: bool | None = None
    for rule in rules:
        action = rule.get("action", "allow")
        matched = True
        if "os" in rule:
            matched = matched and _os_matches(rule["os"], os_info)
        if "features" in rule and matched:
            matched = matched and _features_match(rule["features"], features)
        if matched:
            verdict = (action == "allow")  # last match wins; keep going
    return False if verdict is None else verdict
