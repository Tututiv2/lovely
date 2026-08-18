"""The OS secret store, for refresh tokens.

Section 3.3: the Minecraft access token is short-lived and disposable, but the Microsoft
**refresh token** is the long-lived credential and it does not belong in a JSON file. It
goes to DPAPI on Windows (``CryptProtectData``, current-user scope), Keychain on macOS,
libsecret on Linux.

DPAPI is reached through ``ctypes`` rather than pywin32 so the launcher keeps its
zero-dependency property. The ciphertext is stored base64 in ``accounts.json`` beside the
non-secret profile fields; it is decryptable only by this Windows user on this machine,
which is the property that matters -- copying the file to another PC yields nothing.

An entropy string is mixed in so a blob lifted from this file cannot be decrypted by
another application running as the same user without also knowing it.
"""
from __future__ import annotations

import base64
import ctypes
import ctypes.wintypes as wintypes
import json
import subprocess
import sys

from .. import logs

log = logs.get("accounts.secrets")

ENTROPY = b"MyFireLauncher/refresh-token/v1"
SERVICE = "MyFireLauncher"


class SecretError(RuntimeError):
    pass


# ---------------------------------------------------------------------------------
# Windows DPAPI
# ---------------------------------------------------------------------------------

class _DATA_BLOB(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD),
                ("pbData", ctypes.POINTER(ctypes.c_char))]

    @classmethod
    def make(cls, data: bytes) -> "_DATA_BLOB":
        buf = ctypes.create_string_buffer(data, len(data))
        return cls(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))

    def value(self) -> bytes:
        return ctypes.string_at(self.pbData, self.cbData)


def _dpapi(protect: bool, data: bytes) -> bytes:
    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32
    blob_in = _DATA_BLOB.make(data)
    entropy = _DATA_BLOB.make(ENTROPY)
    blob_out = _DATA_BLOB()
    if protect:
        ok = crypt32.CryptProtectData(
            ctypes.byref(blob_in), SERVICE, ctypes.byref(entropy),
            None, None, 0, ctypes.byref(blob_out))
    else:
        ok = crypt32.CryptUnprotectData(
            ctypes.byref(blob_in), None, ctypes.byref(entropy),
            None, None, 0, ctypes.byref(blob_out))
    if not ok:
        raise SecretError(
            f"DPAPI {'Protect' if protect else 'Unprotect'}Data failed "
            f"(error {ctypes.get_last_error() or kernel32.GetLastError()}). "
            "A stored login cannot be read -- signing in again will replace it.")
    try:
        return blob_out.value()
    finally:
        kernel32.LocalFree(blob_out.pbData)


# ---------------------------------------------------------------------------------
# macOS / Linux
# ---------------------------------------------------------------------------------

def _run(argv: list[str], stdin: bytes | None = None) -> bytes:
    proc = subprocess.run(argv, input=stdin, capture_output=True)
    if proc.returncode != 0:
        raise SecretError(proc.stderr.decode("utf-8", "replace").strip()
                          or f"{argv[0]} failed")
    return proc.stdout


def _keychain_set(key: str, value: bytes) -> None:
    subprocess.run(["security", "delete-generic-password", "-s", SERVICE, "-a", key],
                   capture_output=True)
    _run(["security", "add-generic-password", "-s", SERVICE, "-a", key,
          "-w", value.decode("utf-8"), "-U"])


def _keychain_get(key: str) -> bytes:
    return _run(["security", "find-generic-password", "-s", SERVICE, "-a", key,
                 "-w"]).strip()


def _libsecret_set(key: str, value: bytes) -> None:
    _run(["secret-tool", "store", "--label", f"{SERVICE} {key}",
          "service", SERVICE, "account", key], stdin=value)


def _libsecret_get(key: str) -> bytes:
    return _run(["secret-tool", "lookup", "service", SERVICE, "account", key]).strip()


# ---------------------------------------------------------------------------------
# Public interface
# ---------------------------------------------------------------------------------

def available() -> bool:
    if sys.platform == "win32":
        return True
    try:
        if sys.platform == "darwin":
            subprocess.run(["security", "-h"], capture_output=True, timeout=5)
        else:
            subprocess.run(["secret-tool", "--help"], capture_output=True, timeout=5)
        return True
    except (OSError, subprocess.SubprocessError):
        return False


def seal(key: str, secret: str) -> str:
    """Protect ``secret`` and return an opaque string safe to put in accounts.json."""
    if sys.platform == "win32":
        return "dpapi:" + base64.b64encode(
            _dpapi(True, secret.encode("utf-8"))).decode("ascii")
    if sys.platform == "darwin":
        _keychain_set(key, secret.encode("utf-8"))
        return "keychain:" + key
    _libsecret_set(key, secret.encode("utf-8"))
    return "libsecret:" + key


def unseal(key: str, sealed: str) -> str:
    """Recover a secret sealed by :func:`seal`."""
    scheme, _, payload = sealed.partition(":")
    if scheme == "dpapi":
        return _dpapi(False, base64.b64decode(payload)).decode("utf-8")
    if scheme == "keychain":
        return _keychain_get(payload or key).decode("utf-8")
    if scheme == "libsecret":
        return _libsecret_get(payload or key).decode("utf-8")
    raise SecretError(
        f"Unknown secret scheme {scheme!r}. The stored login cannot be read; sign in "
        "again to replace it.")


def forget(key: str, sealed: str) -> None:
    scheme, _, payload = sealed.partition(":")
    try:
        if scheme == "keychain":
            subprocess.run(["security", "delete-generic-password", "-s", SERVICE,
                            "-a", payload or key], capture_output=True)
        elif scheme == "libsecret":
            subprocess.run(["secret-tool", "clear", "service", SERVICE,
                            "account", payload or key], capture_output=True)
        # dpapi keeps nothing outside the blob itself; dropping the blob is enough.
    except OSError:
        log.debug("could not clear secret for %s", key, exc_info=True)


def self_test() -> bool:
    """Round-trip a value through the real store. Used by ``doctor``."""
    probe = "myfire-selftest-" + "x" * 24
    try:
        sealed = seal("self-test", probe)
        return unseal("self-test", sealed) == probe
    except Exception:
        log.debug("secret store self-test failed", exc_info=True)
        return False
    finally:
        try:
            forget("self-test", "keychain:self-test" if sys.platform == "darwin"
                   else "libsecret:self-test")
        except Exception:
            pass
