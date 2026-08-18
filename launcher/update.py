"""Tell the user when a newer release exists.

It **notifies, it does not self-replace.** A running ``.exe`` cannot overwrite itself on
Windows, so silent self-update means shipping a second helper process that deletes and
replaces the binary that spawned it -- which is indistinguishable, to both antivirus and to
the user, from what malware does. For a free unsigned tool that trade is not worth it. The
launcher checks, says so, and opens the release page.

The check is deliberately cheap and deliberately failure-tolerant: one unauthenticated
GitHub API call, on a background thread, cached for a day. If GitHub is down, rate-limits
us, or the machine is offline, the launcher simply does not mention updates. An update
checker that can interrupt startup is worse than no update checker.
"""
from __future__ import annotations

import json
import re
import time
import webbrowser
from dataclasses import dataclass

from . import logs, net
from .paths import APP_NAME, APP_VERSION

log = logs.get("update")

REPO = "Tututiv2/lovely"
API_LATEST = f"https://api.github.com/repos/{REPO}/releases/latest"
RELEASES_PAGE = f"https://github.com/{REPO}/releases/latest"

CHECK_INTERVAL = 24 * 3600
_VERSION_RE = re.compile(r"(\d+)(?:\.(\d+))?(?:\.(\d+))?")


def parse_version(text: str) -> tuple[int, int, int]:
    """``v1.2.3`` / ``1.2`` / ``Lovely 1.0.0`` -> a comparable tuple. Junk sorts lowest."""
    m = _VERSION_RE.search(text or "")
    if not m:
        return (0, 0, 0)
    return tuple(int(g or 0) for g in m.groups())  # type: ignore[return-value]


def is_newer(candidate: str, current: str = APP_VERSION) -> bool:
    return parse_version(candidate) > parse_version(current)


@dataclass
class Release:
    version: str
    url: str
    name: str = ""
    notes: str = ""
    published: str = ""

    @property
    def headline(self) -> str:
        return f"{APP_NAME} {self.version} is available"


def check(*, timeout: float = 8.0) -> Release | None:
    """Return a newer release, or None. Never raises."""
    try:
        raw = net.get_bytes(
            API_LATEST, timeout=timeout, retries=1,
            headers={"Accept": "application/vnd.github+json",
                     "User-Agent": f"{APP_NAME}/{APP_VERSION}"})
        data = json.loads(raw.decode("utf-8"))
    except (net.NetError, ValueError, KeyError) as exc:
        log.debug("update check failed: %s", exc)
        return None

    if data.get("draft") or data.get("prerelease"):
        return None
    tag = str(data.get("tag_name") or "")
    if not tag or not is_newer(tag):
        return None

    return Release(
        version=tag.lstrip("vV"),
        url=str(data.get("html_url") or RELEASES_PAGE),
        name=str(data.get("name") or ""),
        notes=str(data.get("body") or "")[:400],
        published=str(data.get("published_at") or "")[:10],
    )


def open_release_page(release: Release | None = None) -> bool:
    try:
        return webbrowser.open(release.url if release else RELEASES_PAGE)
    except Exception:
        log.debug("could not open the release page", exc_info=True)
        return False


class UpdateChecker:
    """Rate-limited wrapper so a repaint or a restart cannot spam GitHub."""

    def __init__(self, interval: float = CHECK_INTERVAL) -> None:
        self.interval = interval
        self.release: Release | None = None
        self.dismissed = False
        self._last = 0.0

    @property
    def due(self) -> bool:
        return (time.time() - self._last) > self.interval

    def run(self) -> Release | None:
        self._last = time.time()
        self.release = check()
        if self.release:
            log.info("update available: %s", self.release.version)
        return self.release

    @property
    def should_show(self) -> bool:
        return self.release is not None and not self.dismissed
