"""The main menu -- one full-window canvas, drawn from scratch.

Everything on this screen is canvas geometry rather than tk widgets: rounded cards, a
radial ember glow behind the wordmark, a cool counter-light in the lower right, top and
bottom vignettes, and a field of slowly rising sparks. See :mod:`launcher.ui.canvaskit`
for why each is drawn the way it is.

The screen redraws itself wholesale on resize (debounced), which is far simpler than
maintaining a retained layout and is imperceptible at this item count -- a full rebuild is
roughly 250 canvas items.

Structure:

    header      wordmark, and the account chip
    hero        the last-played instance, with the big Play button
    grid        up to eight instance cards; more than that lives in the Library
    dock        Library / New / Settings / Accounts / Folder
"""
from __future__ import annotations

import time
import tkinter as tk

from .. import logs
from ..paths import APP_NAME
from . import canvaskit as ck
from . import skin, theme

log = logs.get("ui.home")

PAD = 34
MAX_CARDS = 8


class HomeScreen:
    """Owns one Canvas. ``app`` supplies data and takes every action."""

    def __init__(self, master: tk.Widget, app) -> None:
        self.app = app
        self.f = app.f
        self.cv = tk.Canvas(master, bg=theme.BG, highlightthickness=0, bd=0)
        self.embers: ck.EmberField | None = None
        self.animating = True
        self.widgets: list[ck.CanvasWidget] = []
        self._resize_job: str | None = None
        self._last_size = (0, 0)
        self._head_image: tk.PhotoImage | None = None
        self.cv.bind("<Configure>", self._on_configure)

    # ------------------------------------------------------------------ lifecycle
    def pack(self, **kw) -> None:
        self.cv.pack(fill="both", expand=True, **kw)

    def forget(self) -> None:
        self.cv.pack_forget()
        if self.embers:
            self.embers.stop()

    def show(self) -> None:
        self.pack()
        self.redraw()
        self.set_animating(True)

    def set_animating(self, active: bool) -> None:
        """Run the ember field only when it is actually being looked at.

        A launcher spends most of its life behind something else. Animating a canvas it
        cannot be seen on costs about 14% of a core for nothing, which on a laptop is fan
        noise and battery in exchange for an invisible effect.
        """
        self.animating = active
        if self.embers is None:
            return
        if active and self.cv.winfo_ismapped():
            self.embers.start()
        else:
            self.embers.stop()

    def _on_configure(self, event) -> None:
        if (event.width, event.height) == self._last_size:
            return
        self._last_size = (event.width, event.height)
        if self._resize_job is not None:
            self.cv.after_cancel(self._resize_job)
        # Debounced: a window drag emits a Configure per pixel and each redraw is a full
        # rebuild. 90 ms is below the threshold where the delay is noticeable.
        self._resize_job = self.cv.after(90, self.redraw)

    # ------------------------------------------------------------------ painting
    def redraw(self) -> None:
        self._resize_job = None
        cv = self.cv
        w = cv.winfo_width()
        h = cv.winfo_height()
        if w < 50 or h < 50:
            return

        if self.embers:
            self.embers.stop()
        for widget in self.widgets:
            widget.destroy()
        self.widgets.clear()
        cv.delete("all")

        self._paint_backdrop(w, h)
        self._paint_header(w)
        grid_top = self._paint_hero(w, h)
        grid_bottom = self._paint_grid(w, h, grid_top)
        self._paint_runtime_strip(w, h, grid_bottom)
        self._paint_dock(w, h)

        self.embers = ck.EmberField(cv, w, h, count=22, colour=theme.ACCENT,
                                    base=theme.BG, interval_ms=55)
        cv.tag_lower("ember")
        cv.tag_lower("backdrop")
        if self.animating:
            self.embers.start()

    def _paint_backdrop(self, w: int, h: int) -> None:
        cv = self.cv
        cv.create_rectangle(0, 0, w, h, fill=theme.BG, width=0, tags="backdrop")
        # Warm key light behind the wordmark, cool counter-light opposite it. The outer
        # ring of each is exactly the page colour, so neither shows a boundary.
        ck.glow(cv, w * 0.26, h * 0.20, max(w, h) * 0.46, ck.mix(theme.ACCENT_DK, theme.BG, 0.34),
                theme.BG, steps=26, gamma=2.8, squash=0.82, tags="backdrop")
        ck.glow(cv, w * 0.92, h * 0.86, max(w, h) * 0.40, ck.mix(theme.BLUE, theme.BG, 0.80),
                theme.BG, steps=20, gamma=3.0, squash=0.9, tags="backdrop")
        # Vignettes, drawn as scanlines so they blend into the glows above.
        for i in range(35):
            t = i / 34
            cv.create_line(0, i * 2, w, i * 2, width=2,
                           fill=ck.mix(theme.VOID, theme.BG, t), tags="backdrop")
        for i in range(45):
            t = i / 44
            y = h - 1 - i * 2
            cv.create_line(0, y, w, y, width=2,
                           fill=ck.mix(theme.VOID, theme.BG, t), tags="backdrop")

    def _paint_header(self, w: int) -> None:
        cv = self.cv
        y = 40
        # Drawn from APP_NAME so the wordmark can never drift from the registered app name.
        name = APP_NAME.upper()
        cv.create_text(PAD, y, text=name, anchor="w", fill=theme.ACCENT,
                       font=self.f["logo"])
        wordmark = self.f["logo"].measure(name)
        cv.create_text(PAD + wordmark + 10, y, text="LAUNCHER", anchor="w",
                       fill=theme.TEXT, font=self.f["logo_thin"])
        cv.create_text(PAD + 3, y + 26, text=ck.spaced("MINECRAFT JAVA EDITION"),
                       anchor="w", fill=theme.FAINT, font=self.f["kicker"])
        self._paint_account_chip(w)

    def _paint_account_chip(self, w: int) -> None:
        cv = self.cv
        app = self.app
        name, sub, tint = app.account_display()
        chip_w, chip_h = 208, 48
        x = w - PAD - chip_w
        y = 22
        panel = ck.Panel(cv, x, y, chip_w, chip_h, fill=ck.mix(theme.PANEL, theme.BG, 0.2),
                         hover_fill=theme.PANEL_HI, outline=theme.BORDER,
                         hover_outline=theme.BORDER_HI, radius=12,
                         accent=theme.ACCENT, on_click=app.open_accounts)
        self.widgets.append(panel)

        img = app.account_skin_head(28)
        if img is not None:
            self._head_image = img  # keep a reference, or Tk garbage-collects it
            panel.add(cv.create_image(x + 12, y + chip_h / 2, image=img, anchor="w"))
        else:
            # No skin to render (no account, or the dev stub). A drawn monogram tile beats
            # a flat coloured square, and it is the same rounded vocabulary as everything
            # else on this screen.
            for item in ck.emblem(cv, x + 12, y + 10, 28, label=(name[:1] or "?").upper(),
                                  colour=tint if tint != theme.DIM else theme.FAINT,
                                  base=theme.PANEL, font=self.f["emblem"]):
                panel.add(item)
        panel.add(cv.create_text(x + 50, y + 17, text=name, anchor="w", fill=theme.TEXT,
                                 font=self.f["h3"]))
        panel.add(cv.create_text(x + 50, y + 33, text=sub, anchor="w", fill=tint,
                                 font=self.f["tiny"]))

    def _paint_hero(self, w: int, h: int) -> float:
        """The Continue card. Returns the y at which the grid may start."""
        cv = self.cv
        app = self.app
        inst = app.hero_instance()
        x1, y1 = PAD, 108
        card_w = w - PAD * 2
        card_h = 148

        if inst is None:
            panel = ck.Panel(cv, x1, y1, card_w, card_h, fill=theme.PANEL,
                             hover_fill=theme.PANEL, outline=theme.BORDER,
                             hover_outline=theme.BORDER, radius=16, interactive=False)
            self.widgets.append(panel)
            panel.add(cv.create_text(x1 + 30, y1 + 54, anchor="w", fill=theme.TEXT,
                                     font=self.f["h1"], text="No instances yet"))
            panel.add(cv.create_text(
                x1 + 30, y1 + 86, anchor="w", fill=theme.DIM, font=self.f["small"],
                text="Create one and it gets its own mods, saves and config folder.\n"
                     "Nothing in it can reach any other install."))
            self.widgets.append(ck.Button(
                cv, x1 + card_w - 210, y1 + card_h / 2 - 22, 180, 44,
                text="Create an instance", font=self.f["h3"],
                on_click=app.create_instance, fill=theme.ACCENT, hover=theme.ACCENT_HI,
                text_colour=theme.ACCENT_INK, radius=11))
            return y1 + card_h + 34

        tint = theme.loader_colour(inst.loader)
        panel = ck.Panel(cv, x1, y1, card_w, card_h,
                         fill=ck.mix(theme.PANEL, tint, 0.05),
                         hover_fill=ck.mix(theme.PANEL_HI, tint, 0.06),
                         outline=ck.mix(theme.BORDER, tint, 0.22),
                         hover_outline=ck.mix(theme.BORDER_HI, tint, 0.36),
                         radius=16, accent=ck.mix(tint, theme.TEXT, 0.25),
                         on_click=lambda: app.open_library(inst))
        self.widgets.append(panel)
        # A colour rail keyed to the loader, so the card is identifiable at a glance.
        panel.add(ck.round_rect(cv, x1 + 1, y1 + 16, x1 + 5, y1 + card_h - 16, 2,
                                fill=tint, outline=""))

        running = app.is_running(inst.slug)
        panel.add(cv.create_text(x1 + 30, y1 + 30, anchor="w",
                                 text=ck.spaced("NOW PLAYING" if running else "CONTINUE"),
                                 fill=theme.ACCENT if not running else theme.OK,
                                 font=self.f["kicker"]))
        panel.add(cv.create_text(x1 + 28, y1 + 62, anchor="w", text=inst.name,
                                 fill=theme.TEXT, font=self.f["h1"]))

        bits = [inst.mc_version or inst.version_id, inst.loader.title(),
                f"{inst.memory_mb} MB"]
        if inst.quick_play_server:
            bits.append(inst.quick_play_server)
        panel.add(cv.create_text(x1 + 30, y1 + 92, anchor="w", text="   ·   ".join(bits),
                                 fill=theme.DIM, font=self.f["small"]))
        panel.add(cv.create_text(x1 + 30, y1 + 116, anchor="w",
                                 text=app.last_played_text(inst), fill=theme.FAINT,
                                 font=self.f["tiny"]))

        play_w, play_h = 176, 52
        px = x1 + card_w - play_w - 26
        py = y1 + (card_h - play_h) / 2

        # A soft ember bloom behind the primary action. The canvas does not clip, so the
        # radius is clamped to the distance to the nearest card edge -- otherwise the
        # outermost rings spill past the rounded corner and the card looks torn.
        gcx, gcy = px + play_w / 2, py + play_h / 2
        squash = 0.62
        bloom = min(gcx - x1 - 8, x1 + card_w - gcx - 8,
                    (gcy - y1 - 8) / squash, (y1 + card_h - gcy - 8) / squash)
        if bloom > 40:
            ck.glow(cv, gcx, gcy, bloom, ck.mix(theme.ACCENT, theme.PANEL, 0.74),
                    ck.mix(theme.PANEL, tint, 0.05), steps=22, gamma=3.0,
                    squash=squash, tags=panel.tag)
        cv.tag_raise(panel.tag)

        self.play_button = ck.Button(
            cv, px, py, play_w, play_h,
            text="PLAYING" if running else "PLAY", icon="" if running else "▶",
            font=self.f["play"], on_click=lambda: app.play(inst),
            fill=theme.ACCENT, hover=theme.ACCENT_HI, text_colour=theme.ACCENT_INK,
            radius=13)
        self.widgets.append(self.play_button)
        if running or app.busy:
            self.play_button.set_enabled(False, disabled_fill=theme.PANEL_HI,
                                         disabled_text=theme.DIM)

        if inst.quick_play_server and not running:
            join = ck.Button(cv, px - 172, py + 8, 160, 36,
                             text="Play + join server", font=self.f["small"],
                             on_click=lambda: app.play(inst, inst.quick_play_server),
                             fill=ck.mix(theme.PANEL_HI, tint, 0.10),
                             hover=ck.mix(theme.PANEL_SEL, tint, 0.16),
                             text_colour=theme.TEXT, radius=10,
                             outline=ck.mix(theme.BORDER, tint, 0.3),
                             outline_hover=ck.mix(theme.BORDER_HI, tint, 0.4))
            self.widgets.append(join)
            if app.busy:
                join.set_enabled(False)

        return y1 + card_h + 34

    def _paint_grid(self, w: int, h: int, top: float) -> float:
        """Draw the card grid. Returns the y just below the last row."""
        cv = self.cv
        app = self.app
        instances = app.instances_for_menu()
        dock_top = h - 96

        cv.create_text(PAD, top, anchor="w", text=ck.spaced("YOUR INSTANCES"),
                       fill=theme.FAINT, font=self.f["kicker"])
        count = len(instances)
        if count:
            cv.create_text(PAD + 148, top, anchor="w",
                           text=f"{count} installed", fill=theme.FAINT,
                           font=self.f["tiny"])
        self.widgets.append(ck.Button(
            cv, w - PAD - 96, top - 13, 96, 27, text="+  New", font=self.f["small"],
            on_click=app.create_instance, fill=theme.PANEL_HI, hover=theme.PANEL_SEL,
            text_colour=theme.TEXT, radius=8, outline=theme.BORDER,
            outline_hover=theme.BORDER_HI))

        if not instances:
            return top + 26

        cols = 4 if w >= 1000 else (3 if w >= 780 else 2)
        gap = 14
        card_w = (w - PAD * 2 - gap * (cols - 1)) / cols
        card_h = 92
        grid_y = top + 26
        room = max(0, dock_top - grid_y)
        rows = max(1, int((room + gap) // (card_h + gap)))
        shown = instances[:min(MAX_CARDS, rows * cols)]

        for i, inst in enumerate(shown):
            r, c = divmod(i, cols)
            x = PAD + c * (card_w + gap)
            y = grid_y + r * (card_h + gap)
            self._instance_card(inst, x, y, card_w, card_h)

        bottom = grid_y + ((len(shown) - 1) // cols + 1) * (card_h + gap)
        if count > len(shown) and bottom + 26 < dock_top:
            self.widgets.append(ck.Button(
                cv, PAD, bottom, 170, 26, text=f"View all {count}  →",
                font=self.f["small"], on_click=app.open_library,
                fill=theme.BG, hover=theme.PANEL, text_colour=theme.DIM, radius=8))
            bottom += 34
        return bottom

    def _paint_runtime_strip(self, w: int, h: int, top: float) -> None:
        """What the launcher is managing on the user's behalf, in the space under the grid.

        Two facts worth stating on the front page, because both are promises this launcher
        makes and neither is visible anywhere else: which Java runtimes it fetched, and
        that the real ``.minecraft`` is not being touched.
        """
        cv = self.cv
        y = top + 12
        if y > h - 150:
            return  # no room; the dock and the grid win

        cv.create_text(PAD, y, anchor="w", text=ck.spaced("MANAGED FOR YOU"),
                       fill=theme.FAINT, font=self.f["kicker"])
        x = PAD
        y += 24
        for label, installed in self.app.installed_runtimes():
            chip_w = self.f["tiny"].measure(label) + 28
            colour = theme.OK if installed else theme.FAINT
            ck.round_rect(cv, x, y, x + chip_w, y + 22, 7,
                          fill=ck.mix(theme.PANEL, colour, 0.10 if installed else 0.02),
                          outline=ck.mix(theme.BORDER, colour, 0.3 if installed else 0.0),
                          width=1)
            cv.create_oval(x + 9, y + 9, x + 14, y + 14, fill=colour, width=0)
            cv.create_text(x + 20, y + 11, anchor="w", text=label,
                           fill=theme.DIM if installed else theme.FAINT,
                           font=self.f["tiny"])
            x += chip_w + 7
        cv.create_text(x + 4, y + 11, anchor="w", font=self.f["tiny"], fill=theme.FAINT,
                       text="downloaded automatically — no JDK to install")

        cv.create_text(PAD, y + 42, anchor="w", font=self.f["tiny"], fill=theme.FAINT,
                       text="Every instance keeps its own mods, saves and config.  "
                            "%appdata%\\.minecraft is never written to.")

    def _instance_card(self, inst, x: float, y: float, w: float, h: float) -> None:
        cv = self.cv
        app = self.app
        tint = theme.loader_colour(inst.loader)
        running = app.is_running(inst.slug)

        panel = ck.Panel(cv, x, y, w, h,
                         fill=ck.mix(theme.PANEL, tint, 0.035),
                         hover_fill=ck.mix(theme.PANEL_HI, tint, 0.07),
                         outline=ck.mix(theme.BORDER, tint, 0.16),
                         hover_outline=ck.mix(theme.BORDER_HI, tint, 0.42),
                         radius=13, accent=ck.mix(tint, theme.TEXT, 0.2),
                         on_click=lambda i=inst: app.open_library(i))
        self.widgets.append(panel)

        for item in ck.emblem(cv, x + 14, y + 16, 44, label=inst.short_version(),
                              colour=tint, base=theme.PANEL, font=self.f["emblem"]):
            panel.add(item)

        name = inst.name if len(inst.name) <= 22 else inst.name[:21] + "…"
        panel.add(cv.create_text(x + 70, y + 27, anchor="w", text=name, fill=theme.TEXT,
                                 font=self.f["h3"]))
        panel.add(cv.create_text(x + 70, y + 46, anchor="w", fill=theme.DIM,
                                 font=self.f["tiny"],
                                 text=f"{inst.loader.title()}  ·  {inst.memory_mb} MB"))
        panel.add(cv.create_text(x + 70, y + 64, anchor="w",
                                 text="running now" if running
                                 else app.last_played_text(inst),
                                 fill=theme.OK if running else theme.FAINT,
                                 font=self.f["tiny"]))
        if running:
            panel.add(cv.create_oval(x + w - 20, y + 15, x + w - 13, y + 22,
                                     fill=theme.OK, width=0))

    def _paint_dock(self, w: int, h: int) -> None:
        cv = self.cv
        app = self.app
        y = h - 62
        cv.create_line(PAD, y - 18, w - PAD, y - 18, fill=theme.BORDER)

        buttons = [
            ("Library", app.open_library, 96),
            ("Settings", app.open_settings, 92),
            ("Accounts", app.open_accounts, 96),
            ("Folder", app.open_data_folder, 84),
        ]
        x = PAD
        for label, action, bw in buttons:
            self.widgets.append(ck.Button(
                cv, x, y, bw, 34, text=label, font=self.f["small"], on_click=action,
                fill=theme.PANEL, hover=theme.PANEL_HI, text_colour=theme.DIM,
                radius=9, outline=theme.BORDER, outline_hover=theme.BORDER_HI))
            x += bw + 8

        self.status_item = cv.create_text(w - PAD, y + 17, anchor="e",
                                          text=app.status_text, fill=theme.FAINT,
                                          font=self.f["tiny"])

    # ------------------------------------------------------------------ updates
    def set_status(self, text: str) -> None:
        item = getattr(self, "status_item", None)
        if item is not None:
            try:
                self.cv.itemconfigure(item, text=text)
            except tk.TclError:
                pass
