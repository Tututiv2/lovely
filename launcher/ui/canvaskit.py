"""A small drawing kit for building a custom UI on a tkinter Canvas.

tkinter has no rounded corners, no gradients, no alpha and no hover states. All four are
buildable on a Canvas, and doing so is what separates "a tk app" from something that looks
designed. The techniques, and why each was chosen over the obvious alternative:

* **Rounded rectangles** -- a 12-point polygon with ``smooth=True``. Tk's spline rounds the
  corners for free; drawing four arcs plus three rectangles would be seven items per corner
  set and would seam visibly at the joins.
* **Radial glow** -- a stack of concentric ovals whose colour lerps to the *page background*
  at the outer edge. Because the outermost ring is exactly the background colour, the glow
  blends perfectly with no visible boundary, which is the thing a naive version gets wrong.
  Per-pixel compositing into a PhotoImage would be ~800k Python-level operations for a
  full-window backdrop; this is about 50 canvas items and is instant.
* **Vertical gradient** -- one PhotoImage row per scanline, then a single horizontal
  ``zoom``. Setting pixels individually is O(w*h) calls into Tcl and takes seconds.
* **Hover** -- ``tag_bind`` fires ``<Leave>`` when the pointer crosses from a component's
  background onto its own label, which flickers. Every widget here re-checks the pointer
  against its bounding box before believing a leave event.
"""
from __future__ import annotations

import random
import tkinter as tk
from typing import Callable, Sequence

# ---------------------------------------------------------------------------------
# Colour
# ---------------------------------------------------------------------------------

def unhex(colour: str) -> tuple[int, int, int]:
    c = colour.lstrip("#")
    return int(c[0:2], 16), int(c[2:4], 16), int(c[4:6], 16)


def hexc(r: float, g: float, b: float) -> str:
    return "#%02x%02x%02x" % (max(0, min(255, int(r))), max(0, min(255, int(g))),
                              max(0, min(255, int(b))))


def mix(a: str, b: str, t: float) -> str:
    """Blend two colours. ``t=0`` is ``a``, ``t=1`` is ``b``."""
    t = max(0.0, min(1.0, t))
    ar, ag, ab = unhex(a)
    br, bg, bb = unhex(b)
    return hexc(ar + (br - ar) * t, ag + (bg - ag) * t, ab + (bb - ab) * t)


def shade(colour: str, amount: float) -> str:
    """Lighten (``amount`` > 0) or darken (< 0) by a fraction."""
    return mix(colour, "#ffffff" if amount > 0 else "#000000", abs(amount))


def screen(a: str, b: str, strength: float = 1.0) -> str:
    """Screen blend -- how light adds to light. Used for glows over a dark page."""
    ar, ag, ab = unhex(a)
    br, bg, bb = unhex(b)
    f = lambda x, y: 255 - (255 - x) * (255 - y * strength) / 255  # noqa: E731
    return hexc(f(ar, br), f(ag, bg), f(ab, bb))


# ---------------------------------------------------------------------------------
# Shapes
# ---------------------------------------------------------------------------------

def rr_points(x1: float, y1: float, x2: float, y2: float, r: float) -> list[float]:
    r = max(0.0, min(r, (x2 - x1) / 2, (y2 - y1) / 2))
    return [
        x1 + r, y1, x2 - r, y1, x2, y1,
        x2, y1 + r, x2, y2 - r, x2, y2,
        x2 - r, y2, x1 + r, y2, x1, y2,
        x1, y2 - r, x1, y1 + r, x1, y1,
    ]


def round_rect(cv: tk.Canvas, x1: float, y1: float, x2: float, y2: float,
               radius: float = 10, **kw) -> int:
    kw.setdefault("smooth", True)
    kw.setdefault("splinesteps", 16)
    return cv.create_polygon(rr_points(x1, y1, x2, y2, radius), **kw)


def glow(cv: tk.Canvas, cx: float, cy: float, radius: float, colour: str,
         base: str, *, steps: int = 44, gamma: float = 2.4,
         squash: float = 1.0, tags: str | tuple = ()) -> list[int]:
    """A soft radial light. The outermost ring is ``base``, so it blends seamlessly."""
    items = []
    for i in range(steps, 0, -1):
        t = i / steps
        r = radius * t
        items.append(cv.create_oval(
            cx - r, cy - r * squash, cx + r, cy + r * squash,
            fill=mix(colour, base, t ** (1 / gamma)), width=0, tags=tags))
    return items


def vgradient_image(width: int, height: int, top: str, bottom: str,
                    *, gamma: float = 1.0) -> tk.PhotoImage:
    """A vertical gradient, built one scanline at a time then stretched sideways.

    Setting each pixel individually would be width*height calls into Tcl; this is height
    calls plus one zoom, which is the difference between seconds and milliseconds.
    """
    strip = tk.PhotoImage(width=1, height=max(1, height))
    for y in range(height):
        t = (y / max(1, height - 1)) ** gamma
        strip.put(mix(top, bottom, t), to=(0, y, 1, y + 1))
    return strip.zoom(max(1, width), 1)


# ---------------------------------------------------------------------------------
# Ember field
# ---------------------------------------------------------------------------------

class EmberField:
    """Slow-rising sparks. The launcher burns ember-orange; the sparks are the same idea.

    Kept deliberately cheap: positions move with ``Canvas.move`` (which does not re-parse
    coordinates) and colour is only re-set when a particle crosses into a new brightness
    band, because ``itemconfigure`` is the expensive call.

    Even so it is the most expensive thing on the screen, and not for the obvious reason:
    every spark that moves damages a region the canvas must re-composite, and the backdrop
    glows are enormous overlapping ovals, so one 2 px spark can force a stack of them to
    redraw. Measured on this machine: 22% of a core with the backdrop present, 14% without,
    1.6% with the field paused. Hence a modest particle count, a 55 ms tick, and
    :meth:`HomeScreen.set_animating` stopping it whenever the window is not in front.
    """

    BANDS = 6

    def __init__(self, canvas: tk.Canvas, width: int, height: int, *,
                 count: int = 46, colour: str = "#ff7a2f", base: str = "#12141a",
                 tags: str = "ember", interval_ms: int = 55) -> None:
        self.cv = canvas
        self.interval = interval_ms
        self.w, self.h = width, height
        self.tags = tags
        self.colour, self.base = colour, base
        self._palette = [mix(base, colour, (i + 1) / self.BANDS)
                         for i in range(self.BANDS)]
        self._running = False
        self._after: str | None = None
        self.particles: list[dict] = []
        self._spawn(count)

    def _new(self) -> dict:
        return {
            "x": random.uniform(0, max(1, self.w)),
            "y": random.uniform(0, max(1, self.h)),
            "vy": -random.uniform(0.18, 0.75),
            "drift": random.uniform(-0.22, 0.22),
            "phase": random.uniform(0, 6.28),
            "size": random.choice((1.0, 1.0, 1.5, 1.5, 2.0, 2.5)),
            "life": random.uniform(0.2, 1.0),
            "fade": random.uniform(0.0016, 0.0055),
            "band": -1,
            "item": None,
        }

    def _spawn(self, count: int) -> None:
        for _ in range(count):
            p = self._new()
            s = p["size"]
            p["item"] = self.cv.create_oval(p["x"] - s, p["y"] - s, p["x"] + s,
                                            p["y"] + s, width=0, fill=self._palette[0],
                                            tags=self.tags)
            self.particles.append(p)

    def resize(self, width: int, height: int) -> None:
        self.w, self.h = max(1, width), max(1, height)

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._tick()

    def stop(self) -> None:
        self._running = False
        if self._after is not None:
            try:
                self.cv.after_cancel(self._after)
            except Exception:
                pass
            self._after = None

    def _tick(self) -> None:
        if not self._running:
            return
        cv = self.cv
        for p in self.particles:
            p["phase"] += 0.05
            dx = p["drift"] + 0.28 * (random.random() - 0.5)
            dy = p["vy"]
            p["x"] += dx
            p["y"] += dy
            p["life"] -= p["fade"]
            if p["life"] <= 0 or p["y"] < -8 or not (-20 < p["x"] < self.w + 20):
                # Recycle from the bottom rather than allocating a new canvas item.
                p.update(x=random.uniform(0, self.w), y=self.h + random.uniform(2, 40),
                         life=random.uniform(0.7, 1.0),
                         vy=-random.uniform(0.18, 0.75),
                         drift=random.uniform(-0.22, 0.22))
                s = p["size"]
                cv.coords(p["item"], p["x"] - s, p["y"] - s, p["x"] + s, p["y"] + s)
            else:
                cv.move(p["item"], dx, dy)
            band = min(self.BANDS - 1, max(0, int(p["life"] * self.BANDS)))
            if band != p["band"]:
                p["band"] = band
                cv.itemconfigure(p["item"], fill=self._palette[band])
        try:
            self._after = cv.after(self.interval, self._tick)
        except tk.TclError:
            self._running = False


# ---------------------------------------------------------------------------------
# Widgets
# ---------------------------------------------------------------------------------

_uid = [0]


def _next_tag(prefix: str) -> str:
    _uid[0] += 1
    return f"{prefix}{_uid[0]}"


class CanvasWidget:
    """Base class: a tagged group of canvas items with reliable hover and click.

    **The hit region is the declared geometry, never the live bounding box.** A bbox is
    computed from the items themselves, so it changes whenever a hover effect changes them
    -- and a hover effect that moves the widget then moves it out from under the pointer,
    which makes Tk fire ``<Leave>``, which undoes the effect, which puts it back under the
    pointer, which fires ``<Enter>``. A stationary mouse resting on an edge spins that loop
    as fast as Tk can dispatch, the main thread never returns to the event loop, and Windows
    kills the process for not responding (Application Error 1002 -- a *hang*, which looks
    exactly like a crash from outside). That is not hypothetical: it shipped, and it is why
    hover here changes colour only and the hit test reads :attr:`geom`.
    """

    def __init__(self, canvas: tk.Canvas, prefix: str = "w") -> None:
        self.cv = canvas
        self.tag = _next_tag(prefix)
        self.items: list[int] = []
        self.hovered = False
        self.enabled = True
        #: (x, y, w, h) at creation. Fixed for the widget's lifetime.
        self.geom: tuple[float, float, float, float] | None = None
        self._on_click: Callable[[], None] | None = None
        self._bound = False
        self._in_hover = False

    # -- item helpers ---------------------------------------------------------------
    def add(self, item: int) -> int:
        self.items.append(item)
        self.cv.addtag_withtag(self.tag, item)
        return item

    def bbox(self) -> tuple[int, int, int, int] | None:
        return self.cv.bbox(self.tag)

    def hit_box(self) -> tuple[float, float, float, float] | None:
        """The stationary rectangle used for hit testing. Never derived from the items."""
        if self.geom is None:
            return self.bbox()
        x, y, w, h = self.geom
        return (x, y, x + w, y + h)

    def contains(self, event) -> bool:
        box = self.hit_box()
        if not box:
            return False
        x = self.cv.canvasx(event.x)
        y = self.cv.canvasy(event.y)
        return box[0] <= x <= box[2] and box[1] <= y <= box[3]

    def destroy(self) -> None:
        self.cv.delete(self.tag)
        self.items.clear()

    def lift(self) -> None:
        self.cv.tag_raise(self.tag)

    # -- interaction -----------------------------------------------------------------
    def bind_events(self, on_click: Callable[[], None] | None = None) -> None:
        self._on_click = on_click
        if self._bound:
            return
        self._bound = True
        self.cv.tag_bind(self.tag, "<Enter>", self._enter)
        self.cv.tag_bind(self.tag, "<Leave>", self._leave)
        self.cv.tag_bind(self.tag, "<Button-1>", self._press)
        self.cv.tag_bind(self.tag, "<ButtonRelease-1>", self._release)

    def _set_hover(self, hovering: bool) -> None:
        if self.hovered == hovering or self._in_hover:
            return
        self.hovered = hovering
        self._in_hover = True          # a hover effect must never re-enter this
        try:
            self.cv.configure(cursor="hand2" if hovering else "")
            self.on_hover(hovering)
        finally:
            self._in_hover = False

    def _enter(self, event=None) -> None:
        # Gated on the same fixed rectangle as _leave. With both ends of the state machine
        # reading one stationary box, "pointer is inside" is a single consistent fact and
        # enter/leave cannot disagree about it -- which is what makes a loop impossible
        # rather than merely unlikely.
        if self.enabled and (event is None or self.contains(event)):
            self._set_hover(True)

    def _leave(self, event=None) -> None:
        # Tk also fires Leave when the pointer crosses from this widget's background onto
        # its own label, so only believe it once the pointer is outside the fixed hit box.
        if event is not None and self.contains(event):
            return
        self._set_hover(False)

    def _press(self, _event=None) -> None:
        if self.enabled:
            self.on_press(True)

    def _release(self, event=None) -> None:
        if not self.enabled:
            return
        self.on_press(False)
        if self._on_click and self.contains(event):
            self._on_click()

    # -- overridable ------------------------------------------------------------------
    def on_hover(self, hovering: bool) -> None:
        pass

    def on_press(self, pressed: bool) -> None:
        pass


class Button(CanvasWidget):
    """A rounded button drawn on the canvas, with hover, press and disabled states."""

    def __init__(self, canvas: tk.Canvas, x: float, y: float, w: float, h: float, *,
                 text: str, font, on_click: Callable[[], None] | None = None,
                 fill: str, hover: str, text_colour: str, radius: float = 9,
                 outline: str = "", outline_hover: str = "", icon: str = "") -> None:
        super().__init__(canvas, "btn")
        self.geom = (x, y, w, h)
        self.colours = (fill, hover, text_colour, outline, outline_hover or outline)
        label = f"{icon}  {text}" if icon else text
        self.bg = self.add(round_rect(canvas, x, y, x + w, y + h, radius, fill=fill,
                                      outline=outline or fill,
                                      width=1 if outline else 0))
        self.label = self.add(canvas.create_text(x + w / 2, y + h / 2 + 0.5, text=label,
                                                 fill=text_colour, font=font))
        self.bind_events(on_click)

    def on_hover(self, hovering: bool) -> None:
        fill, hover, _, outline, outline_hover = self.colours
        self.cv.itemconfigure(self.bg, fill=hover if hovering else fill,
                              outline=(outline_hover if hovering else outline)
                              or (hover if hovering else fill))

    def on_press(self, pressed: bool) -> None:
        fill, hover, _, _, _ = self.colours
        self.cv.itemconfigure(self.bg, fill=shade(hover if self.hovered else fill,
                                                  -0.12 if pressed else 0))

    def set_enabled(self, enabled: bool, *, disabled_fill: str = "#191c24",
                    disabled_text: str = "#626a79") -> None:
        self.enabled = enabled
        fill, _, text_colour, outline, _ = self.colours
        self.cv.itemconfigure(self.bg, fill=fill if enabled else disabled_fill,
                              outline=(outline or fill) if enabled else disabled_fill)
        self.cv.itemconfigure(self.label, fill=text_colour if enabled else disabled_text)

    def set_text(self, text: str) -> None:
        self.cv.itemconfigure(self.label, text=text)


class Panel(CanvasWidget):
    """A rounded surface that brightens its fill and edge on hover.

    It deliberately does **not** move. An earlier version lifted by 3 px, which was the
    direct cause of a hang: see :class:`CanvasWidget` for the loop. A colour-only hover
    cannot change what is under the pointer, so the feedback loop cannot exist.
    """

    def __init__(self, canvas: tk.Canvas, x: float, y: float, w: float, h: float, *,
                 fill: str, hover_fill: str, outline: str, hover_outline: str,
                 radius: float = 14, on_click: Callable[[], None] | None = None,
                 interactive: bool = True, accent: str | None = None) -> None:
        super().__init__(canvas, "panel")
        self.geom = (x, y, w, h)
        self.radius = radius
        self.colours = (fill, hover_fill, outline, hover_outline)
        self.bg = self.add(round_rect(canvas, x, y, x + w, y + h, radius, fill=fill,
                                      outline=outline, width=1))
        # A second, inset outline that only appears on hover. Two rings read as clearly as
        # a 3 px jump did, and cost nothing but a colour change.
        self.ring = self.add(round_rect(canvas, x + 1.5, y + 1.5, x + w - 1.5,
                                        y + h - 1.5, max(1.0, radius - 1.5),
                                        fill="", outline=fill, width=1))
        self.accent = accent
        if interactive:
            self.bind_events(on_click)

    def on_hover(self, hovering: bool) -> None:
        fill, hover_fill, outline, hover_outline = self.colours
        self.cv.itemconfigure(self.bg, fill=hover_fill if hovering else fill,
                              outline=hover_outline if hovering else outline)
        # At rest the ring is painted the same colour as the surface behind it, so it is
        # invisible without needing to be created and destroyed.
        self.cv.itemconfigure(
            self.ring,
            outline=(self.accent or hover_outline) if hovering else fill)


def emblem(canvas: tk.Canvas, x: float, y: float, size: float, *, label: str,
           colour: str, base: str, font, tags: str | tuple = ()) -> list[int]:
    """A small square badge with a two-tone wash -- stands in for per-version artwork."""
    items = [round_rect(canvas, x, y, x + size, y + size, size * 0.28,
                        fill=mix(colour, base, 0.62), outline=mix(colour, base, 0.35),
                        width=1, tags=tags)]
    items.append(round_rect(canvas, x, y, x + size, y + size * 0.52, size * 0.28,
                            fill=mix(colour, base, 0.44), outline="", width=0, tags=tags))
    items.append(canvas.create_text(x + size / 2, y + size / 2, text=label,
                                    fill=shade(colour, 0.45), font=font, tags=tags))
    return items


def spaced(text: str, gap: str = " ") -> str:
    """Fake letter-spacing. Tk fonts have no tracking, and section labels need it."""
    return gap.join(text)
