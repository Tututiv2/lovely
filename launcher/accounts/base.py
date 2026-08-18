"""The ``Account`` interface -- the only thing the launch pipeline is allowed to read.

Section 3.4 of the brief: *nothing in the launch pipeline may reach for a token directly.*
That single rule is what lets the dev stub and a real Microsoft account be swapped without
touching one line of :mod:`launcher.launch`, and it is why real auth (milestone 2) can land
weeks late without a rewrite.

Implementations live beside this file:

* :class:`launcher.accounts.local.LocalAccount` -- the flag-gated dev stub (section 3.5).
* :class:`launcher.accounts.msa.MicrosoftAccount` -- the real five-hop chain (section 3).
"""
from __future__ import annotations

import abc
from dataclasses import dataclass


class Account(abc.ABC):
    """What a launch needs to know about who is playing."""

    #: Human-readable, shown in the UI.
    name: str
    #: Profile UUID, dashless form is what goes on the command line.
    uuid: str

    @property
    @abc.abstractmethod
    def access_token(self) -> str:
        """The Minecraft session token. May trigger a silent refresh."""

    @property
    @abc.abstractmethod
    def user_type(self) -> str:
        """``msa`` for a real account, ``legacy`` for the dev stub."""

    @property
    def xuid(self) -> str:
        return ""

    @property
    def online(self) -> bool:
        """True if this account can pass a server's session check."""
        return False

    @property
    def uuid_dashless(self) -> str:
        return self.uuid.replace("-", "")

    @property
    def uuid_dashed(self) -> str:
        u = self.uuid_dashless
        if len(u) != 32:
            return self.uuid
        return f"{u[0:8]}-{u[8:12]}-{u[12:16]}-{u[16:20]}-{u[20:32]}"

    def summary(self) -> "AccountSummary":
        return AccountSummary(self.name, self.uuid_dashless, self.user_type, self.online)

    def __repr__(self) -> str:  # never let a token reach a repr
        return f"<{type(self).__name__} {self.name} {self.uuid_dashless[:8]}...>"


@dataclass(frozen=True)
class AccountSummary:
    """Token-free view, safe to log, serialise and show."""
    name: str
    uuid: str
    user_type: str
    online: bool


class AuthError(RuntimeError):
    """Base for every authentication failure, carrying text meant for a human."""

    def __init__(self, message: str, *, detail: str = "", code: str = ""):
        self.detail = detail
        self.code = code
        super().__init__(message)
