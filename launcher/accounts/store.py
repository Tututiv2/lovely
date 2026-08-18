"""Multiple accounts, with the refresh token in the OS secret store.

``accounts.json`` holds only what is safe to read: display name, UUID, XUID, when it was
last used, and an *opaque sealed blob* produced by :mod:`launcher.accounts.secrets`. On
Windows that blob is DPAPI ciphertext bound to this user on this machine, so the file is
useless if copied anywhere else. The Minecraft access token is never written to disk at
all -- it lives ~24 hours and is re-derived from the refresh token on demand.

Accounts are keyed by profile UUID, which is what makes the switcher stable when someone
renames themselves.
"""
from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field, fields

from .. import logs, net
from ..paths import Layout, ext
from . import secrets
from .base import Account, AuthError
from .msa import MicrosoftAccount, restore, sign_in

log = logs.get("accounts.store")


@dataclass
class StoredAccount:
    uuid: str
    name: str
    xuid: str = ""
    sealed_refresh: str = ""
    last_used: float = 0.0
    skin_url: str = ""

    @property
    def secret_key(self) -> str:
        return f"account-{self.uuid}"


class AccountStore:
    """Persisted accounts plus the currently active one."""

    def __init__(self, layout: Layout, client_id: str = "") -> None:
        self.layout = layout
        self.client_id = client_id
        self.accounts: list[StoredAccount] = []
        self.active_uuid: str = ""
        self._live: dict[str, MicrosoftAccount] = {}
        self.load()

    # -- persistence ---------------------------------------------------------------
    def load(self) -> None:
        try:
            with open(ext(self.layout.accounts_file), "r", encoding="utf-8-sig") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            return
        known = {f.name for f in fields(StoredAccount)}
        self.accounts = [
            StoredAccount(**{k: v for k, v in a.items() if k in known})
            for a in data.get("accounts", [])
        ]
        self.active_uuid = data.get("active", "")

    def save(self) -> None:
        payload = {"accounts": [asdict(a) for a in self.accounts],
                   "active": self.active_uuid}
        net.write_atomic(self.layout.accounts_file,
                         json.dumps(payload, indent=2).encode("utf-8"))

    # -- lookup ---------------------------------------------------------------------
    def get(self, uuid: str) -> StoredAccount | None:
        return next((a for a in self.accounts if a.uuid == uuid), None)

    @property
    def active(self) -> StoredAccount | None:
        return self.get(self.active_uuid) or (self.accounts[0] if self.accounts else None)

    def set_active(self, uuid: str) -> None:
        self.active_uuid = uuid
        acct = self.get(uuid)
        if acct:
            acct.last_used = time.time()
        self.save()

    # -- mutation -------------------------------------------------------------------
    def _remember(self, account: MicrosoftAccount) -> StoredAccount:
        entry = self.get(account.uuid) or StoredAccount(uuid=account.uuid,
                                                        name=account.name)
        entry.name = account.name
        entry.xuid = account.xuid
        entry.last_used = time.time()
        entry.skin_url = account.skin_url() or entry.skin_url
        try:
            entry.sealed_refresh = secrets.seal(entry.secret_key,
                                                account.refresh_token)
        except secrets.SecretError as exc:
            raise AuthError(
                f"Signed in, but the refresh token could not be stored securely: {exc} "
                "You will have to sign in again next time.", code="secret_store") from exc
        if entry not in self.accounts:
            self.accounts = [a for a in self.accounts if a.uuid != entry.uuid] + [entry]
        self.active_uuid = entry.uuid
        self._live[entry.uuid] = account
        self.save()
        return entry

    def add_microsoft(self, **sign_in_kwargs) -> MicrosoftAccount:
        """Run the interactive device-code flow and persist the result."""
        account = sign_in(self.client_id, **sign_in_kwargs)
        self._remember(account)
        return account

    def remove(self, uuid: str) -> None:
        entry = self.get(uuid)
        if entry is None:
            return
        if entry.sealed_refresh:
            secrets.forget(entry.secret_key, entry.sealed_refresh)
        self.accounts = [a for a in self.accounts if a.uuid != uuid]
        self._live.pop(uuid, None)
        if self.active_uuid == uuid:
            self.active_uuid = self.accounts[0].uuid if self.accounts else ""
        self.save()

    # -- use ------------------------------------------------------------------------
    def resolve(self, uuid: str | None = None) -> Account:
        """Return a launch-ready account, refreshing eagerly rather than on failure."""
        entry = self.get(uuid) if uuid else self.active
        if entry is None:
            raise AuthError("No account is signed in.", code="no_account")

        live = self._live.get(entry.uuid)
        if live is not None:
            live.ensure_fresh()
            self._remember(live)
            return live

        if not entry.sealed_refresh:
            raise AuthError(
                f"No stored login for {entry.name}. Sign in again.", code="no_secret")
        try:
            token = secrets.unseal(entry.secret_key, entry.sealed_refresh)
        except secrets.SecretError as exc:
            raise AuthError(
                f"The saved login for {entry.name} could not be read ({exc}). "
                "Sign in again to replace it.", code="secret_unreadable") from exc

        account = restore(self.client_id, token)
        self._remember(account)
        return account

    def summaries(self) -> list[dict]:
        """Token-free rows for the account switcher."""
        return [{"uuid": a.uuid, "name": a.name, "active": a.uuid == self.active_uuid,
                 "skin_url": a.skin_url} for a in self.accounts]
