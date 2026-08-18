"""The dev stub -- local testing only, while Azure approval is pending.

Section 3.5 of the brief. This exists so milestones 1 and 3-8 can be built and proven
against a server running ``online-mode=false`` before the Azure app is approved for the
Minecraft API. It **cannot** connect to any online-mode server: the server asks Mojang to
confirm the session and there is nothing here to confirm.

It is gated behind ``--dev`` / ``MYFIRE_DEV=1`` and is never offered as a normal UI option.
Once real auth works this can be deleted outright; nothing else imports it.

The UUID derivation matches how a vanilla server derives one for an offline player --
UUID v3 (MD5) over UTF-8 ``OfflinePlayer:<name>``, with the version and variant bits forced
per RFC 4122 -- so save data and permissions follow the name rather than resetting.
"""
from __future__ import annotations

import hashlib
import os
import uuid as _uuid

from .base import Account, AuthError

DEV_ENV_FLAG = "MYFIRE_DEV"

#: Flipped to True by ``tools/package.py --public`` when building a build for strangers.
#:
#: A flag-gated offline profile is a reasonable development tool when the only person who
#: can reach it is the person who wrote it. Hand the same build to an audience and it stops
#: being a development tool: someone will find ``--dev``, and the app's own approval
#: submission says there is no offline mode. Public builds therefore cannot enable it at
#: all -- not "it is hidden", but the gate is welded shut.
PUBLIC_BUILD = True


def offline_uuid(username: str) -> str:
    """UUID v3 of ``OfflinePlayer:<username>``, dashed. Matches Mojang's server-side rule."""
    digest = bytearray(hashlib.md5(
        f"OfflinePlayer:{username}".encode("utf-8")).digest())
    digest[6] = (digest[6] & 0x0F) | 0x30   # version 3
    digest[8] = (digest[8] & 0x3F) | 0x80   # RFC 4122 variant
    return str(_uuid.UUID(bytes=bytes(digest)))


def dev_mode_enabled() -> bool:
    if PUBLIC_BUILD:
        return False
    return os.environ.get(DEV_ENV_FLAG, "").strip() not in ("", "0", "false", "False")


class LocalAccount(Account):
    """Offline identity for local servers. Refuses to exist outside dev mode."""

    def __init__(self, username: str, *, force: bool = False) -> None:
        if not force and not dev_mode_enabled():
            raise AuthError(
                "The dev account is only available in dev mode. Start the launcher with "
                "--dev (or set MYFIRE_DEV=1) if you are testing against a local server "
                "with online-mode=false.",
                code="dev_disabled")
        username = (username or "").strip()
        if not (1 <= len(username) <= 16) or not all(
                c.isalnum() or c == "_" for c in username):
            raise AuthError(
                f"{username!r} is not a valid Minecraft username (1-16 characters, "
                "letters, digits and underscore).", code="bad_username")
        self.name = username
        self.uuid = offline_uuid(username)

    @property
    def access_token(self) -> str:
        return "0"

    @property
    def user_type(self) -> str:
        return "legacy"

    @property
    def xuid(self) -> str:
        return ""

    @property
    def online(self) -> bool:
        return False
