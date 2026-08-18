"""Player heads, rendered from the account's real skin.

No image library involved. Tk 8.6's photo image understands PNG natively, and its
underlying ``copy`` subcommand takes ``-from`` (crop) and ``-zoom`` (nearest-neighbour
scale) -- which is exactly a Minecraft head: the 8x8 face at (8,8) with the 8x8 hat layer
at (40,8) composited on top, scaled up with hard pixel edges.

Nearest neighbour is the right filter here, not a limitation: a smoothed 8x8 face looks
like a smudge.
"""
from __future__ import annotations

import tkinter as tk
from pathlib import Path

from .. import logs, net
from ..paths import Layout, ext, mkdirs
from . import theme

log = logs.get("ui.skin")

FACE = (8, 8, 16, 16)
HAT = (40, 8, 48, 16)


def cache_path(layout: Layout, uuid: str) -> Path:
    return layout.meta_cache / "skins" / f"{uuid}.png"


def fetch_skin(layout: Layout, uuid: str, url: str) -> Path | None:
    """Download a skin PNG into the meta cache. Returns None if it cannot be had."""
    if not url:
        return None
    dest = cache_path(layout, uuid)
    try:
        if not dest.is_file():
            mkdirs(dest.parent)
            net.download(url, dest)
        return dest
    except net.NetError as exc:
        log.debug("skin fetch failed for %s: %s", uuid, exc)
        return None


def head_image(png_path: Path, size: int = 32) -> tk.PhotoImage | None:
    """Crop the face, composite the hat, and scale to ``size``. None if unreadable."""
    try:
        src = tk.PhotoImage(file=str(png_path))
    except tk.TclError as exc:
        log.debug("could not read skin %s: %s", png_path, exc)
        return None
    if src.width() < 64 or src.height() < 32:
        return None
    zoom = max(1, size // 8)
    out = tk.PhotoImage(width=8 * zoom, height=8 * zoom)
    try:
        out.tk.call(out, "copy", src, "-from", *FACE, "-to", 0, 0, "-zoom", zoom)
        # The hat layer is transparent where unused, and Tk's copy honours the alpha
        # channel, so compositing is a second copy rather than a blend.
        out.tk.call(out, "copy", src, "-from", *HAT, "-to", 0, 0, "-zoom", zoom,
                    "-compositingrule", "overlay")
    except tk.TclError as exc:
        log.debug("head render failed: %s", exc)
        return None
    return out


def placeholder_head(name: str, size: int = 32) -> tk.PhotoImage:
    """A deterministic flat tile for accounts with no skin (notably the dev stub)."""
    img = tk.PhotoImage(width=size, height=size)
    h = 0
    for ch in name or "?":
        h = (h * 31 + ord(ch)) & 0xFFFFFF
    r, g, b = 60 + (h & 0x3F), 60 + ((h >> 8) & 0x3F), 70 + ((h >> 16) & 0x3F)
    img.put(f"#{r:02x}{g:02x}{b:02x}", to=(0, 0, size, size))
    accent = theme.ACCENT if (h & 1) else theme.BLUE
    img.put(accent, to=(0, size - max(2, size // 10), size, size))
    return img
