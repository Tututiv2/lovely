"""Colours, fonts and the plain-widget helpers the dialogs and library are built from.

Tk is used with explicit colours on plain widgets rather than ttk styling, because ttk on
Windows refuses to honour background colours on several widget classes and the result is a
dark app with light grey buttons. Everything here is drawn deliberately.

The launcher is stdlib-only on purpose -- no pip install, no venv to rot, and the .bat the
owner double-clicks keeps working forever. tkinter ships with Python; PySide6 would have
been prettier out of the box and would also have been a 200 MB dependency that can break.
The gap is closed by drawing the main menu ourselves (see :mod:`launcher.ui.canvaskit`).
"""
from __future__ import annotations

import tkinter as tk
import tkinter.font as tkfont

# -- surfaces ---------------------------------------------------------------------
VOID = "#0b0d11"      # behind everything; the deepest tone
BG = "#12141a"        # page
PANEL = "#191c24"     # raised surface
PANEL_HI = "#22262f"  # hovered / input
PANEL_SEL = "#2b303c"  # selected
BORDER = "#272c36"
BORDER_HI = "#3d4553"

# -- ink --------------------------------------------------------------------------
TEXT = "#edeff3"
DIM = "#9aa3b2"
FAINT = "#626a79"

# -- accents ----------------------------------------------------------------------
ACCENT = "#ff7a2f"     # ember, the product accent
ACCENT_HI = "#ffa15c"
ACCENT_DK = "#c9541a"
ACCENT_INK = "#1a0f05"  # text that sits on an ember fill
BLUE = "#4d9dff"
VIOLET = "#a874e8"
OK = "#5ccf7f"
WARN = "#e8b13c"
ERR = "#ef5f57"

LOADER_COLOURS = {
    # Vanilla is deliberately the dimmest: every card is tinted with its loader colour,
    # and a bright neutral makes plain instances shout louder than modded ones.
    "vanilla": "#5c6575",
    "fabric": "#d8ac3c",
    "quilt": "#a874e8",
    "forge": "#5b8fe0",
    "neoforge": "#ef7f45",
}


def loader_colour(loader: str) -> str:
    return LOADER_COLOURS.get((loader or "vanilla").lower(), FAINT)


def fonts() -> dict:
    return {
        "logo": tkfont.Font(family="Segoe UI Black", size=27),
        "logo_thin": tkfont.Font(family="Segoe UI Light", size=27),
        "kicker": tkfont.Font(family="Segoe UI Semibold", size=8),
        "h1": tkfont.Font(family="Segoe UI Semibold", size=20),
        "h2": tkfont.Font(family="Segoe UI Semibold", size=13),
        "h3": tkfont.Font(family="Segoe UI Semibold", size=11),
        "body": tkfont.Font(family="Segoe UI", size=10),
        "small": tkfont.Font(family="Segoe UI", size=9),
        "tiny": tkfont.Font(family="Segoe UI", size=8),
        "mono": tkfont.Font(family="Consolas", size=9),
        "code": tkfont.Font(family="Consolas", size=20),
        "play": tkfont.Font(family="Segoe UI Semibold", size=13),
        "emblem": tkfont.Font(family="Segoe UI Semibold", size=9),
    }


class Button(tk.Label):
    """A flat, hover-lit button. tk.Button cannot be coloured reliably on Windows."""

    def __init__(self, master, text: str, command=None, *, kind: str = "normal",
                 font=None, padx: int = 14, pady: int = 7, width: int | None = None,
                 **kw):
        self.kind = kind
        self._command = command
        self._enabled = True
        colours = {
            "normal": (PANEL_HI, TEXT, PANEL_SEL),
            "primary": (ACCENT, ACCENT_INK, ACCENT_HI),
            "ghost": (PANEL, DIM, PANEL_HI),
            "danger": (PANEL_HI, ERR, "#3a2626"),
        }[kind]
        self._bg, self._fg, self._hover = colours
        super().__init__(master, text=text, bg=self._bg, fg=self._fg, padx=padx,
                         pady=pady, cursor="hand2", font=font, **kw)
        if width:
            self.configure(width=width)
        self.bind("<Enter>", self._on_enter)
        self.bind("<Leave>", self._on_leave)
        self.bind("<Button-1>", self._on_click)

    def _on_enter(self, _=None):
        if self._enabled:
            self.configure(bg=self._hover)

    def _on_leave(self, _=None):
        self.configure(bg=self._bg if self._enabled else PANEL)

    def _on_click(self, _=None):
        if self._enabled and self._command:
            self._command()

    def set_command(self, command) -> None:
        self._command = command

    def set_enabled(self, enabled: bool) -> None:
        self._enabled = enabled
        self.configure(bg=self._bg if enabled else PANEL,
                       fg=self._fg if enabled else FAINT,
                       cursor="hand2" if enabled else "arrow")


class ProgressBar(tk.Canvas):
    """A two-tone bar drawn by hand, so it can be honest about indeterminate phases."""

    def __init__(self, master, width: int = 320, height: int = 5, **kw):
        super().__init__(master, width=width, height=height, bg=PANEL,
                         highlightthickness=0, bd=0, **kw)
        # NB: never name these `_w` / `_h` -- `_w` is Tk's own widget path attribute and
        # overwriting it turns every later canvas call into `invalid command name "320"`.
        self._px_w = width
        self._px_h = height
        self._track = self.create_rectangle(0, 0, width, height, fill=BORDER, width=0)
        self._fill = self.create_rectangle(0, 0, 0, height, fill=ACCENT, width=0)
        self.bind("<Configure>", self._resize)
        self._fraction = 0.0

    def _resize(self, event):
        self._px_w = event.width
        self.coords(self._track, 0, 0, self._px_w, self._px_h)
        self.set(self._fraction)

    def set(self, fraction: float) -> None:
        self._fraction = max(0.0, min(1.0, fraction))
        self.coords(self._fill, 0, 0, self._px_w * self._fraction, self._px_h)

    def set_colour(self, colour: str) -> None:
        self.itemconfigure(self._fill, fill=colour)


def badge(master, text: str, colour: str, font) -> tk.Label:
    return tk.Label(master, text=f" {text} ", bg=PANEL_HI, fg=colour, font=font)


def hline(master, colour: str = BORDER) -> tk.Frame:
    return tk.Frame(master, bg=colour, height=1)


def scrollable(master, bg: str = PANEL):
    """A vertically scrolling frame. Returns (outer, inner)."""
    outer = tk.Frame(master, bg=bg)
    canvas = tk.Canvas(outer, bg=bg, highlightthickness=0, bd=0)
    bar = tk.Scrollbar(outer, orient="vertical", command=canvas.yview)
    inner = tk.Frame(canvas, bg=bg)
    window = canvas.create_window((0, 0), window=inner, anchor="nw")

    def on_config(_=None):
        canvas.configure(scrollregion=canvas.bbox("all"))
        canvas.itemconfigure(window, width=canvas.winfo_width())

    inner.bind("<Configure>", on_config)
    canvas.bind("<Configure>", on_config)
    canvas.configure(yscrollcommand=bar.set)
    canvas.pack(side="left", fill="both", expand=True)
    bar.pack(side="right", fill="y")

    def wheel(event):
        canvas.yview_scroll(-1 * (event.delta // 120), "units")

    canvas.bind_all("<MouseWheel>", wheel, add="+")
    return outer, inner
