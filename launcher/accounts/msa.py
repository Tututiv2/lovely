"""Microsoft -> Xbox Live -> XSTS -> Minecraft. Five hops, five failure modes.

Each hop is its own function so each can be tested in isolation with a mocked transport,
which is the only practical way to cover the error paths -- you cannot make a real account
be a child account on demand.

Two distinctions in here are the difference between a five-minute diagnosis and a
six-week one:

* **403 from ``api.minecraftservices.com`` is an approval status, not a bug.** Microsoft
  gates the Minecraft API behind a manual review (https://aka.ms/mce-reviewappid). Until
  the Azure app is approved every call to that host 403s, while the Microsoft and Xbox Live
  hops keep working perfectly -- which is exactly what makes it confusing.
* **404 from ``/minecraft/profile`` means the account does not own Java Edition.** A
  Bedrock-only or Game Pass account. Completely different problem, completely different
  fix, and conflating the two sends people down the wrong path for days.

The device code flow is used rather than an embedded browser: nothing to ship, no
credentials ever touching this process, and it works when the browser is on another
machine. ``authorization_pending`` during polling is **normal** -- treating it as an error
is the classic bug that makes login look broken.
"""
from __future__ import annotations

import time
import webbrowser
from dataclasses import dataclass
from typing import Callable

from .. import logs, net
from .base import Account, AuthError

log = logs.get("accounts.msa")

DEVICE_CODE_URL = "https://login.microsoftonline.com/consumers/oauth2/v2.0/devicecode"
TOKEN_URL = "https://login.microsoftonline.com/consumers/oauth2/v2.0/token"
XBL_URL = "https://user.auth.xboxlive.com/user/authenticate"
XSTS_URL = "https://xsts.auth.xboxlive.com/xsts/authorize"
MC_LOGIN_URL = "https://api.minecraftservices.com/authentication/login_with_xbox"
MC_ENTITLEMENTS_URL = "https://api.minecraftservices.com/entitlements/mcstore"
MC_PROFILE_URL = "https://api.minecraftservices.com/minecraft/profile"

SCOPE = "XboxLive.signin offline_access"
APPROVAL_URL = "https://aka.ms/mce-reviewappid"

#: Only the tenant that works. ``common`` and directory tenant ids both fail with the
#: XboxLive scope, with an error that does not say so.
TENANT_NOTE = "consumers"


# ---------------------------------------------------------------------------------
# Named errors
# ---------------------------------------------------------------------------------

class AppNotApprovedError(AuthError):
    """403 from the Minecraft API: the Azure app has not been approved yet."""

    def __init__(self, url: str = ""):
        super().__init__(
            "This launcher's Microsoft app has not been approved for the Minecraft API "
            f"yet (403). That is an approval status, not a bug -- apply or check at "
            f"{APPROVAL_URL}. Everything except online login works meanwhile; use a dev "
            "account against a local server until it lands.",
            code="app_not_approved", detail=url)


class DoesNotOwnGameError(AuthError):
    def __init__(self, name: str = ""):
        super().__init__(
            "This Microsoft account does not own Minecraft: Java Edition. That is usually "
            "a Bedrock-only or Game Pass account -- Java Edition is a separate purchase. "
            "Sign in with the account that owns Java Edition.",
            code="no_entitlement", detail=name)


class DeviceCodeExpired(AuthError):
    def __init__(self):
        super().__init__("The sign-in code expired before it was used. Start again.",
                         code="expired_token")


class DeviceCodeDeclined(AuthError):
    def __init__(self):
        super().__init__("Sign-in was declined in the browser.",
                         code="authorization_declined")


#: XSTS refuses with a numeric XErr. Users cannot act on a bare number.
XERR_MESSAGES = {
    "2148916233": ("This Microsoft account has no Xbox profile. Sign in once at "
                   "xbox.com to create one, then try again."),
    "2148916235": ("Xbox Live is not available in this account's region."),
    "2148916236": ("This account needs adult verification (South Korea)."),
    "2148916237": ("This account needs adult verification (South Korea)."),
    "2148916238": ("This account is registered to someone under 18 and must be added to "
                   "a Microsoft Family group by an adult before it can sign in."),
}


class XstsError(AuthError):
    def __init__(self, xerr: str, raw: dict):
        message = XERR_MESSAGES.get(
            xerr, f"Xbox Live refused the sign-in (XErr {xerr or 'unknown'}).")
        super().__init__(message, code=f"xerr_{xerr}", detail=str(raw.get("Message", "")))
        self.xerr = xerr


# ---------------------------------------------------------------------------------
# Hop 0: device code
# ---------------------------------------------------------------------------------

@dataclass
class DeviceCode:
    device_code: str
    user_code: str
    verification_uri: str
    expires_in: int
    interval: int
    message: str = ""
    issued: float = 0.0

    @property
    def expires_at(self) -> float:
        return (self.issued or time.time()) + self.expires_in

    @property
    def seconds_left(self) -> float:
        return max(0.0, self.expires_at - time.time())

    def open_browser(self) -> bool:
        try:
            return webbrowser.open(self.verification_uri)
        except Exception:
            return False


def begin_device_code(client_id: str) -> DeviceCode:
    """Ask Microsoft for a user code. The user types it at ``verification_uri``."""
    if not client_id:
        raise AuthError(
            "No Azure client ID configured. Register an app (see the README's first "
            "section), then put its Application (client) ID in Settings.",
            code="no_client_id")
    resp = net.post_json(DEVICE_CODE_URL,
                         form={"client_id": client_id, "scope": SCOPE},
                         expect=(200,))
    data = resp.json()
    code = DeviceCode(
        device_code=data["device_code"],
        user_code=data["user_code"],
        verification_uri=data.get("verification_uri")
        or data.get("verification_url", "https://microsoft.com/link"),
        expires_in=int(data.get("expires_in", 900)),
        interval=max(1, int(data.get("interval", 5))),
        message=data.get("message", ""),
        issued=time.time(),
    )
    logs.register_secret(code.device_code)
    return code


def poll_device_code(client_id: str, code: DeviceCode, *,
                     on_wait: Callable[[float], None] | None = None,
                     cancel: net.CancelToken = net.NEVER) -> dict:
    """Poll until the user finishes in the browser.

    ``authorization_pending`` is the expected response for as long as the user is typing;
    ``slow_down`` means raise the interval. Neither is a failure.
    """
    interval = float(code.interval)
    deadline = time.time() + code.expires_in
    while True:
        cancel.check()
        if time.time() > deadline:
            raise DeviceCodeExpired()
        time.sleep(interval)
        if on_wait:
            on_wait(max(0.0, deadline - time.time()))
        resp = net.post_json(
            TOKEN_URL,
            form={"grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                  "client_id": client_id, "device_code": code.device_code},
            expect=(200, 400, 401, 403), retries=3)
        data = resp.json()
        if resp.status == 200:
            logs.register_secret(data.get("access_token"))
            logs.register_secret(data.get("refresh_token"))
            return data
        error = data.get("error", "")
        if error == "authorization_pending":
            continue
        if error == "slow_down":
            interval += 5
            continue
        if error == "expired_token":
            raise DeviceCodeExpired()
        if error in ("authorization_declined", "access_denied"):
            raise DeviceCodeDeclined()
        raise AuthError(
            f"Microsoft rejected the sign-in: {data.get('error_description') or error}",
            code=error)


def refresh_microsoft_token(client_id: str, refresh_token: str) -> dict:
    resp = net.post_json(
        TOKEN_URL,
        form={"grant_type": "refresh_token", "client_id": client_id,
              "refresh_token": refresh_token, "scope": SCOPE},
        expect=(200, 400, 401))
    data = resp.json()
    if resp.status != 200:
        raise AuthError(
            "The saved sign-in is no longer valid; sign in again. "
            f"({data.get('error_description') or data.get('error', 'unknown')})",
            code=data.get("error", "refresh_failed"))
    logs.register_secret(data.get("access_token"))
    logs.register_secret(data.get("refresh_token"))
    return data


# ---------------------------------------------------------------------------------
# Hops 1-5
# ---------------------------------------------------------------------------------

def xbox_live(ms_access_token: str) -> tuple[str, str]:
    """Hop 1. Returns ``(xbl_token, user_hash)``. Keep the hash -- hop 3 needs it."""
    resp = net.post_json(XBL_URL, {
        "Properties": {"AuthMethod": "RPS", "SiteName": "user.auth.xboxlive.com",
                       "RpsTicket": f"d={ms_access_token}"},
        "RelyingParty": "http://auth.xboxlive.com",
        "TokenType": "JWT",
    }, expect=(200,))
    data = resp.json()
    token = data.get("Token")
    claims = (data.get("DisplayClaims") or {}).get("xui") or [{}]
    uhs = claims[0].get("uhs", "")
    if not token or not uhs:
        raise AuthError("Xbox Live returned no token for this account.",
                        code="xbl_no_token")
    logs.register_secret(token)
    return token, uhs


def xsts(xbl_token: str) -> tuple[str, str, str]:
    """Hop 2. Returns ``(xsts_token, user_hash, xuid)``. Translates XErr codes."""
    resp = net.post_json(XSTS_URL, {
        "Properties": {"SandboxId": "RETAIL", "UserTokens": [xbl_token]},
        "RelyingParty": "rp://api.minecraftservices.com/",
        "TokenType": "JWT",
    }, expect=(200, 401))
    data = resp.json()
    if resp.status == 401:
        raise XstsError(str(data.get("XErr", "")), data)
    token = data.get("Token")
    claims = (data.get("DisplayClaims") or {}).get("xui") or [{}]
    if not token:
        raise AuthError("Xbox Live (XSTS) returned no token.", code="xsts_no_token")
    logs.register_secret(token)
    return token, claims[0].get("uhs", ""), claims[0].get("xid", "")


def minecraft_login(user_hash: str, xsts_token: str) -> dict:
    """Hop 3. The token this returns is the one that goes on the command line."""
    try:
        resp = net.post_json(
            MC_LOGIN_URL,
            {"identityToken": f"XBL3.0 x={user_hash};{xsts_token}"},
            expect=(200,))
    except net.HttpError as exc:
        if exc.status == 403:
            raise AppNotApprovedError(MC_LOGIN_URL) from exc
        raise
    data = resp.json()
    logs.register_secret(data.get("access_token"))
    return data


def check_entitlements(mc_access_token: str) -> list[dict]:
    """Hop 4. An empty ``items`` array means this account does not own Java Edition."""
    try:
        resp = net.request("GET", MC_ENTITLEMENTS_URL,
                           headers={"Authorization": f"Bearer {mc_access_token}"},
                           expect=(200,))
    except net.HttpError as exc:
        if exc.status == 403:
            raise AppNotApprovedError(MC_ENTITLEMENTS_URL) from exc
        raise
    items = resp.json().get("items") or []
    if not items:
        raise DoesNotOwnGameError()
    return items


def get_profile(mc_access_token: str) -> dict:
    """Hop 5. 404 here means "does not own the game" -- *not* the same as a 403."""
    try:
        resp = net.request("GET", MC_PROFILE_URL,
                           headers={"Authorization": f"Bearer {mc_access_token}"},
                           expect=(200,))
    except net.HttpError as exc:
        if exc.status == 403:
            raise AppNotApprovedError(MC_PROFILE_URL) from exc
        if exc.status == 404:
            raise DoesNotOwnGameError() from exc
        raise
    data = resp.json()
    if not data.get("id") or not data.get("name"):
        raise AuthError("Minecraft returned a profile with no id or name.",
                        code="bad_profile")
    return data


# ---------------------------------------------------------------------------------
# The account
# ---------------------------------------------------------------------------------

#: Refresh this far ahead of expiry. Section 3.3: refresh eagerly on launch, never lazily
#: on failure -- an expired token discovered halfway through a 600 MB download is awful.
REFRESH_MARGIN = 15 * 60


class MicrosoftAccount(Account):
    """A real, entitled Minecraft account. The only kind that can join an online server."""

    def __init__(self, *, client_id: str, name: str, uuid: str, xuid: str,
                 mc_token: str, mc_expires_at: float, refresh_token: str,
                 skins: list | None = None) -> None:
        self.client_id = client_id
        self.name = name
        self.uuid = uuid
        self._xuid = xuid
        self._mc_token = mc_token
        self._expires_at = mc_expires_at
        self._refresh_token = refresh_token
        self.skins = skins or []
        logs.register_secret(mc_token)
        logs.register_secret(refresh_token)

    # -- Account interface ---------------------------------------------------------
    @property
    def access_token(self) -> str:
        self.ensure_fresh()
        return self._mc_token

    @property
    def user_type(self) -> str:
        return "msa"

    @property
    def xuid(self) -> str:
        return self._xuid

    @property
    def online(self) -> bool:
        return True

    @property
    def refresh_token(self) -> str:
        return self._refresh_token

    @property
    def expires_at(self) -> float:
        return self._expires_at

    @property
    def stale(self) -> bool:
        return time.time() >= self._expires_at - REFRESH_MARGIN

    def skin_url(self) -> str | None:
        for skin in self.skins:
            if skin.get("state", "ACTIVE") == "ACTIVE" and skin.get("url"):
                return skin["url"]
        return None

    # -- refresh --------------------------------------------------------------------
    def ensure_fresh(self) -> None:
        if not self.stale:
            return
        log.info("refreshing session for %s", self.name)
        self._reauth()

    def _reauth(self) -> None:
        data = refresh_microsoft_token(self.client_id, self._refresh_token)
        self._refresh_token = data.get("refresh_token") or self._refresh_token
        fresh = complete_chain(self.client_id, data["access_token"])
        self._mc_token = fresh.mc_token
        self._expires_at = fresh.mc_expires_at
        self._xuid = fresh.xuid or self._xuid
        self.name = fresh.name
        self.uuid = fresh.uuid
        self.skins = fresh.skins


@dataclass
class ChainResult:
    name: str
    uuid: str
    xuid: str
    mc_token: str
    mc_expires_at: float
    skins: list


def complete_chain(client_id: str, ms_access_token: str) -> ChainResult:
    """Hops 1-5 in order, from a Microsoft access token to a verified profile."""
    xbl_token, _ = xbox_live(ms_access_token)
    xsts_token, uhs, xuid = xsts(xbl_token)
    mc = minecraft_login(uhs, xsts_token)
    token = mc["access_token"]
    check_entitlements(token)
    profile = get_profile(token)
    return ChainResult(
        name=profile["name"],
        uuid=profile["id"],
        xuid=xuid,
        mc_token=token,
        mc_expires_at=time.time() + float(mc.get("expires_in", 86400)),
        skins=profile.get("skins") or [],
    )


def sign_in(client_id: str, *,
            on_code: Callable[[DeviceCode], None] | None = None,
            on_wait: Callable[[float], None] | None = None,
            cancel: net.CancelToken = net.NEVER) -> MicrosoftAccount:
    """The whole interactive flow. ``on_code`` shows the user code and opens the browser."""
    code = begin_device_code(client_id)
    log.info("device code issued; user must visit %s", code.verification_uri)
    if on_code:
        on_code(code)
    tokens = poll_device_code(client_id, code, on_wait=on_wait, cancel=cancel)
    result = complete_chain(client_id, tokens["access_token"])
    log.info("signed in as %s", result.name)
    return MicrosoftAccount(
        client_id=client_id, name=result.name, uuid=result.uuid, xuid=result.xuid,
        mc_token=result.mc_token, mc_expires_at=result.mc_expires_at,
        refresh_token=tokens["refresh_token"], skins=result.skins)


def restore(client_id: str, refresh_token: str) -> MicrosoftAccount:
    """Bring a saved account back without user interaction."""
    data = refresh_microsoft_token(client_id, refresh_token)
    result = complete_chain(client_id, data["access_token"])
    return MicrosoftAccount(
        client_id=client_id, name=result.name, uuid=result.uuid, xuid=result.xuid,
        mc_token=result.mc_token, mc_expires_at=result.mc_expires_at,
        refresh_token=data.get("refresh_token") or refresh_token, skins=result.skins)
