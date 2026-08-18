"""Logging, with token redaction applied at the logging layer.

Constraint #2 of the brief: never log an access token or a refresh token -- not at debug
level, not in a crash dump, not in the log the user is about to paste somewhere. Doing that
at every call site is a promise you break the first time you add a call site, so it is done
*here* instead, in one filter that every handler shares.

Two mechanisms, because either alone leaks:

* **Registered secrets.** Anything the auth code actually holds is registered with
  :func:`register_secret` the moment it exists, and is replaced by an opaque marker wherever
  it shows up in a log record -- message, args, or exception text.
* **Shape matching.** Tokens we never saw (a raw response body echoed from an error path,
  a JWT inside someone else's JSON) are caught by pattern: ``Bearer``, ``XBL3.0 x=..``,
  ``"access_token": "..."``, and bare three-segment JWTs.

:func:`redact` is exported so non-logging paths (writing a support bundle, showing an HTTP
body in an error dialog) can run text through the same scrubber.
"""
from __future__ import annotations

import logging
import logging.handlers
import re
import sys
import threading
from pathlib import Path

MARK = "<redacted>"

_lock = threading.Lock()
_secrets: set[str] = set()

# Shape-based catches, applied after registered-secret substitution.
_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    # {"access_token": "...."} / 'refresh_token' / 'identityToken' / 'RpsTicket' / 'Token'
    (re.compile(
        r'("(?:access_token|refresh_token|id_token|identityToken|device_code|'
        r'RpsTicket|Token|accessToken)"\s*:\s*")([^"]{8,})(")',
        re.IGNORECASE),
     r"\1" + MARK + r"\3"),
    # Authorization: Bearer <jwt>
    (re.compile(r"(Bearer\s+)[A-Za-z0-9._~+/=-]{20,}", re.IGNORECASE),
     r"\1" + MARK),
    # XBL3.0 x=<uhs>;<xsts token>
    (re.compile(r"(XBL3\.0\s+x=)[^;\s]+;\S+", re.IGNORECASE),
     r"\1" + MARK),
    # A bare JWT anywhere: three dot-separated base64url runs.
    (re.compile(r"\bey[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b"),
     MARK),
    # Minecraft access tokens are long opaque base64url blobs without dots; catch the
    # ones we handed to a command line by their flag.
    (re.compile(r"(--accessToken\s+)\S+"), r"\1" + MARK),
)


def register_secret(value: str | None) -> None:
    """Remember ``value`` so it is scrubbed from every future log record."""
    if not value or len(value) < 8:
        return
    with _lock:
        _secrets.add(value)


def forget_secrets() -> None:
    with _lock:
        _secrets.clear()


def redact(text: str) -> str:
    """Scrub registered secrets and token-shaped substrings out of ``text``."""
    if not text:
        return text
    with _lock:
        known = tuple(_secrets)
    for s in known:
        if s in text:
            text = text.replace(s, MARK)
    for pattern, repl in _PATTERNS:
        text = pattern.sub(repl, text)
    return text


class RedactingFilter(logging.Filter):
    """Rewrites the record in place so *every* handler downstream sees clean text."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            if isinstance(record.msg, str):
                record.msg = redact(record.msg)
            if record.args:
                if isinstance(record.args, dict):
                    record.args = {
                        k: (redact(v) if isinstance(v, str) else v)
                        for k, v in record.args.items()
                    }
                else:
                    record.args = tuple(
                        redact(a) if isinstance(a, str) else a for a in record.args
                    )
            if record.exc_text:
                record.exc_text = redact(record.exc_text)
        except Exception:  # never let redaction failure drop a log line
            record.msg = "<redaction failed; message suppressed>"
            record.args = ()
        return True


class _RedactingFormatter(logging.Formatter):
    """Belt and braces: exception tracebacks are rendered here, after the filter ran."""

    def format(self, record: logging.LogRecord) -> str:
        return redact(super().format(record))


_configured = False


def setup(log_dir: Path | None = None, level: int = logging.INFO,
          console: bool = True) -> logging.Logger:
    """Configure the ``launcher`` logger tree once. Safe to call repeatedly."""
    global _configured
    root = logging.getLogger("launcher")
    if _configured:
        return root
    root.setLevel(logging.DEBUG)
    root.propagate = False
    filt = RedactingFilter()
    fmt = _RedactingFormatter(
        "%(asctime)s %(levelname)-7s %(name)-22s %(message)s", "%H:%M:%S")

    # Under pythonw.exe there is no console and ``sys.stderr`` is None, so a StreamHandler
    # would fail on every single record -- silently, because logging swallows its own
    # errors. Checking here is what keeps the file handler below the *only* sink that
    # matters for the double-clickable build.
    if console and sys.stderr is not None:
        ch = logging.StreamHandler()
        ch.setLevel(level)
        ch.setFormatter(fmt)
        ch.addFilter(filt)
        root.addHandler(ch)

    if log_dir is not None:
        from .paths import mkdirs, ext
        mkdirs(log_dir)
        fh = logging.handlers.RotatingFileHandler(
            ext(Path(log_dir) / "launcher.log"), maxBytes=2_000_000,
            backupCount=3, encoding="utf-8")
        fh.setLevel(logging.DEBUG)
        fh.setFormatter(fmt)
        fh.addFilter(filt)
        root.addHandler(fh)

    _configured = True
    return root


def get(name: str) -> logging.Logger:
    return logging.getLogger(f"launcher.{name}")
