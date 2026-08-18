"""Launcher-wide settings, persisted as plain JSON next to the data root.

Deliberately small. Anything that varies per instance lives in ``instance.json`` instead;
this is only the defaults a new instance inherits and the handful of app-level toggles.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path

from . import logs, net
from .paths import Layout, ext

log = logs.get("settings")

#: The Azure application (client) ID this build ships with.
#:
#: A client ID is public by design -- it identifies the *application*, not any user, and it
#: is sent in the clear on every sign-in request. Baking it in is what lets someone unzip
#: the launcher and sign in with their own Microsoft account without being handed a GUID to
#: paste. One app registration serves every copy; each person authenticates as themselves.
#:
#: Leave empty for an unbranded build; Settings still accepts a per-machine override.
DEFAULT_CLIENT_ID = "8dd9fdce-fb57-40f9-ad29-8ff28630e259"


@dataclass
class Settings:
    #: Default max heap for a new instance. 4 GB, not "half of RAM": large heaps make GC
    #: pauses worse, and Minecraft rarely benefits above 8 GB.
    default_memory_mb: int = 4096
    #: Bounded, per section 10. More threads against Windows real-time scanning is slower.
    download_concurrency: int = 12
    close_on_launch: bool = False
    java_override: str = ""
    show_snapshots: bool = False
    show_old_versions: bool = False
    theme: str = "dark"
    #: Azure application (client) ID. Taken from config, never hardcoded from elsewhere.
    client_id: str = ""
    last_instance: str = ""
    #: Off-by-default, explicitly editable. Not a wall of forum GC flags.
    extra_jvm_args: list[str] = field(default_factory=list)

    @property
    def effective_client_id(self) -> str:
        """The client ID to authenticate with: this machine's override, else the built-in.

        Everything that signs in reads this rather than :attr:`client_id`, so a shared copy
        of the launcher works out of the box while still letting someone point their own
        install at their own app registration.
        """
        return self.client_id.strip() or DEFAULT_CLIENT_ID

    @classmethod
    def load(cls, layout: Layout) -> "Settings":
        try:
            with open(ext(layout.settings_file), "r", encoding="utf-8-sig") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            return cls()
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in data.items() if k in known})

    def save(self, layout: Layout) -> None:
        net.write_atomic(layout.settings_file,
                         json.dumps(asdict(self), indent=2).encode("utf-8"))
