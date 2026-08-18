"""The desktop app -- a thin client of the core.

Two screens live in one window: :class:`launcher.ui.home.HomeScreen` (the main menu, drawn
entirely on a canvas) and the Library below (instance list, detail, log console). ``App``
owns all the state and every action; the screens only arrange it and call back in. That
split is what lets the main menu be a full custom repaint without any launch logic knowing.

Every long operation -- resolving, downloading, installing a loader, signing in -- runs on
a worker thread and reports back through :meth:`App._on_ui`, which marshals onto the Tk
thread with ``after``. The window stays responsive while the game runs, and closing the
launcher does not close the game.

Nothing in here computes anything about Minecraft. If a behaviour matters it lives in the
core and is covered by ``tests/run_tests.py``; this file only puts it on screen.
"""
from __future__ import annotations

import os
import queue
import subprocess
import sys
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox

from .. import libraries, logs, nbt, net
from ..accounts import Account, AccountStore, AuthError, LocalAccount, dev_mode_enabled
from ..instances import Instance, InstanceStore
from ..launch import GameProcess, LaunchError, prepare, spawn
from ..loaders import install_loader
from ..paths import APP_NAME, APP_VERSION, Layout
from ..settings import Settings
from . import canvaskit as ck
from . import skin, theme
from .dialogs import (CreateInstanceDialog, EditInstanceDialog, SettingsDialog,
                      SignInDialog)
from .home import HomeScreen
from .watchdog import (Watchdog, install_tk_error_handler,
                       redirect_stdio)

log = logs.get("ui")


def _dark_titlebar(window: tk.Misc) -> None:
    """Ask DWM for a dark title bar so the frame matches the app.

    Purely cosmetic and entirely optional -- wrapped because the attribute id changed
    between Windows 10 builds and an older build simply ignores it.
    """
    if sys.platform != "win32":
        return
    try:
        import ctypes
        window.update_idletasks()
        hwnd = ctypes.windll.user32.GetParent(window.winfo_id())
        value = ctypes.c_int(1)
        for attribute in (20, 19):  # 20 on 20H1+, 19 on earlier builds
            if ctypes.windll.dwmapi.DwmSetWindowAttribute(
                    hwnd, attribute, ctypes.byref(value),
                    ctypes.sizeof(value)) == 0:
                break
    except Exception:
        log.debug("dark title bar unavailable", exc_info=True)


class App:
    def __init__(self, layout: Layout) -> None:
        self.layout = layout
        self.layout.ensure()
        libraries.sweep_stale_natives(layout.natives_root)
        self.settings = Settings.load(layout)
        self.instances = InstanceStore(layout)
        self.accounts = AccountStore(layout, self.settings.effective_client_id)
        self.dev_account: LocalAccount | None = None
        self.selected: Instance | None = None
        self.running: dict[str, GameProcess] = {}
        self.cancel: net.CancelToken | None = None
        self.busy = False
        self.status_text = "Ready"
        self.screen = "home"
        self._ui_queue: queue.Queue = queue.Queue()
        self._main_thread = threading.current_thread()
        self._head_cache: dict[str, tk.PhotoImage] = {}
        self._cached_instances: list[Instance] = []

        self.root = tk.Tk()
        self.root.title(f"{APP_NAME} {APP_VERSION}")
        self.root.geometry("1180x740")
        self.root.minsize(900, 620)
        self.root.configure(bg=theme.BG)
        _dark_titlebar(self.root)
        self.f = theme.fonts()

        self._build()
        # Diagnostics before anything else can go wrong: a double-clicked app has no
        # console, so it has to be able to explain its own failures. See ui/watchdog.py.
        install_tk_error_handler(self.root, self._show_crash)
        self.watchdog = Watchdog()
        self.watchdog.start()

        self.root.after(60, self._pump)
        self.reload_instances()
        self.show_home()
        log.info("launcher ready (%d instances)", len(self._cached_instances))

    # ================================================================== construction
    def _build(self) -> None:
        self.stack = tk.Frame(self.root, bg=theme.BG)
        self.stack.pack(fill="both", expand=True)

        self.home = HomeScreen(self.stack, self)
        self.library = tk.Frame(self.stack, bg=theme.BG)
        self._build_library()

        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.root.bind("<Escape>", lambda _e: self.show_home())
        # Pause the menu's animation whenever the window is not the one being looked at.
        # Focus events name whichever *child* gained or lost focus, not the toplevel, so
        # the event itself cannot answer "does this app have focus" -- Tk is asked instead,
        # after the event settles.
        for sequence in ("<FocusIn>", "<FocusOut>", "<Map>", "<Unmap>"):
            self.root.bind(sequence, self._focus_changed, add="+")

    # ---------------------------------------------------------------- library chrome
    def _build_library(self) -> None:
        top = tk.Frame(self.library, bg=theme.PANEL, height=56)
        top.pack(side="top", fill="x")
        top.pack_propagate(False)

        theme.Button(top, "◀  Menu", self.show_home, kind="ghost",
                     font=self.f["small"]).pack(side="left", padx=(14, 0), pady=11)
        tk.Label(top, text="LIBRARY", bg=theme.PANEL, fg=theme.FAINT,
                 font=self.f["kicker"]).pack(side="left", padx=16)

        self.lib_account = tk.Label(top, text="", bg=theme.PANEL, fg=theme.DIM,
                                    font=self.f["small"], cursor="hand2")
        self.lib_account.pack(side="right", padx=16)
        self.lib_account.bind("<Button-1>", lambda _e: self.open_accounts())

        tk.Frame(self.library, bg=theme.BORDER, height=1).pack(fill="x")

        body = tk.Frame(self.library, bg=theme.BG)
        body.pack(fill="both", expand=True)

        left = tk.Frame(body, bg=theme.PANEL, width=300)
        left.pack(side="left", fill="y")
        left.pack_propagate(False)

        header = tk.Frame(left, bg=theme.PANEL)
        header.pack(fill="x", padx=14, pady=(14, 6))
        tk.Label(header, text=ck.spaced("INSTANCES"), bg=theme.PANEL, fg=theme.FAINT,
                 font=self.f["kicker"]).pack(side="left")
        theme.Button(header, "+ New", self.create_instance, kind="ghost",
                     font=self.f["small"], padx=8, pady=2).pack(side="right")

        self.list_outer, self.list_inner = theme.scrollable(left, theme.PANEL)
        self.list_outer.pack(fill="both", expand=True, padx=6)

        tk.Frame(left, bg=theme.BORDER, height=1).pack(fill="x")
        foot = tk.Frame(left, bg=theme.PANEL)
        foot.pack(fill="x", padx=14, pady=9)
        theme.Button(foot, "Settings", self.open_settings, kind="ghost",
                     font=self.f["small"], padx=8, pady=3).pack(side="left")
        theme.Button(foot, "Folder", self.open_data_folder, kind="ghost",
                     font=self.f["small"], padx=8, pady=3).pack(side="right")

        self.right = tk.Frame(body, bg=theme.BG)
        self.right.pack(side="left", fill="both", expand=True)
        self._build_detail()

        tk.Frame(self.library, bg=theme.BORDER, height=1).pack(fill="x")
        bottom = tk.Frame(self.library, bg=theme.PANEL)
        bottom.pack(side="bottom", fill="x")

        row = tk.Frame(bottom, bg=theme.PANEL)
        row.pack(fill="x", padx=16, pady=(9, 4))
        self.status = tk.Label(row, text="Ready", bg=theme.PANEL, fg=theme.DIM,
                               font=self.f["small"], anchor="w")
        self.status.pack(side="left")
        self.cancel_btn = theme.Button(row, "Cancel", self.cancel_work, kind="ghost",
                                       font=self.f["small"], padx=8, pady=2)
        self.console_btn = theme.Button(row, "Console", self.toggle_console, kind="ghost",
                                        font=self.f["small"], padx=8, pady=2)
        self.console_btn.pack(side="right")
        self.progress = theme.ProgressBar(bottom)
        self.progress.pack(fill="x", padx=16, pady=(0, 9))

        self.console_frame = tk.Frame(bottom, bg=theme.VOID)
        self.console = tk.Text(self.console_frame, bg=theme.VOID, fg="#c8ccd4",
                               font=self.f["mono"], height=12, wrap="none",
                               insertbackground=theme.TEXT, bd=0, highlightthickness=0,
                               padx=10, pady=6)
        cbar = tk.Scrollbar(self.console_frame, command=self.console.yview)
        self.console.configure(yscrollcommand=cbar.set, state="disabled")
        self.console.pack(side="left", fill="both", expand=True)
        cbar.pack(side="right", fill="y")
        self.console_visible = False
        # "plain" exists because Tk's Text widget *inherits* tags when text is inserted
        # between two characters that both carry one -- so an untagged line appended after
        # a warning comes out amber. Every insert names its tag explicitly.
        self.console.tag_configure("plain", foreground="#c8ccd4")
        self.console.tag_configure("err", foreground=theme.ERR)
        self.console.tag_configure("warn", foreground=theme.WARN)
        self.console.tag_configure("launcher", foreground=theme.ACCENT)

    def _build_detail(self) -> None:
        for w in self.right.winfo_children():
            w.destroy()

        self.detail = tk.Frame(self.right, bg=theme.BG)
        self.detail.pack(fill="both", expand=True, padx=26, pady=22)

        # A short loader-coloured tick, not a full-width rule: a rule reads as a stray
        # line, a tick reads as a label for the heading beneath it.
        rail_row = tk.Frame(self.detail, bg=theme.BG)
        rail_row.pack(fill="x", pady=(0, 12))
        self.hero_rail = tk.Frame(rail_row, bg=theme.ACCENT, height=3, width=54)
        self.hero_rail.pack(side="left")
        self.hero_rail.pack_propagate(False)

        self.title_lbl = tk.Label(self.detail, text="", bg=theme.BG, fg=theme.TEXT,
                                  font=self.f["h1"], anchor="w")
        self.title_lbl.pack(fill="x")

        self.badges = tk.Frame(self.detail, bg=theme.BG)
        self.badges.pack(fill="x", pady=(8, 18))

        self.play_row = tk.Frame(self.detail, bg=theme.BG)
        self.play_row.pack(fill="x")
        self.play_btn = theme.Button(self.play_row, "  ▶   Play  ", self.play,
                                     kind="primary", font=self.f["play"],
                                     padx=26, pady=10)
        self.play_btn.pack(side="left")
        self.join_btn = theme.Button(self.play_row, "Play + join server", self.play_join,
                                     kind="normal", font=self.f["body"])
        self.join_btn.pack(side="left", padx=8)

        tk.Frame(self.detail, bg=theme.BG, height=18).pack()

        actions = tk.Frame(self.detail, bg=theme.BG)
        actions.pack(fill="x")
        for label, fn in (("Edit", self.edit_instance),
                          ("Mods", lambda: self.open_sub("mods")),
                          ("Saves", lambda: self.open_sub("saves")),
                          ("Logs", lambda: self.open_sub("logs")),
                          ("Open folder", lambda: self.open_sub("")),
                          ("Duplicate", self.duplicate_instance),
                          ("Export", self.export_instance)):
            theme.Button(actions, label, fn, kind="normal",
                         font=self.f["small"]).pack(side="left", padx=(0, 6))
        theme.Button(actions, "Delete", self.delete_instance, kind="danger",
                     font=self.f["small"]).pack(side="right")

        tk.Frame(self.detail, bg=theme.BORDER, height=1).pack(fill="x", pady=18)

        self.info = tk.Frame(self.detail, bg=theme.BG)
        self.info.pack(fill="both", expand=True)

    # ================================================================== navigation
    def show_home(self) -> None:
        self.screen = "home"
        self.library.pack_forget()
        self.reload_instances()
        self.home.show()

    def open_library(self, inst: Instance | None = None) -> None:
        self.screen = "library"
        self.home.forget()
        self.library.pack(fill="both", expand=True)
        if inst is not None:
            self.select(inst)
        elif self.selected is None:
            self._select_initial()
        else:
            self.refresh_instances()
            self.show_detail()
        self.refresh_account()

    def _focus_changed(self, _event=None) -> None:
        self.root.after_idle(self._apply_animation_state)

    def _apply_animation_state(self) -> None:
        try:
            has_focus = self.root.focus_displayof() is not None
            mapped = self.root.winfo_ismapped() and self.root.state() != "iconic"
        except (tk.TclError, KeyError):
            return
        self.home.set_animating(has_focus and mapped)

    def refresh_current_screen(self) -> None:
        if self.screen == "home":
            self.reload_instances()
            self.home.redraw()
        else:
            self.refresh_instances()
            self.show_detail()

    # ================================================================== data for home
    def reload_instances(self) -> None:
        self._cached_instances = self.instances.all()

    def instances_for_menu(self) -> list[Instance]:
        return self._cached_instances

    def hero_instance(self) -> Instance | None:
        """Whatever is running, else the last played, else the first one there is."""
        for inst in self._cached_instances:
            if self.is_running(inst.slug):
                return inst
        if self.selected is not None:
            current = self.instances.get(self.selected.slug)
            if current is not None:
                return current
        return self._cached_instances[0] if self._cached_instances else None

    def is_running(self, slug: str) -> bool:
        proc = self.running.get(slug)
        return proc is not None and proc.running

    def last_played_text(self, inst: Instance) -> str:
        if not inst.last_played:
            return "never played"
        delta = time.time() - inst.last_played
        if delta < 90:
            return "played just now"
        if delta < 3600:
            return f"played {int(delta // 60)} min ago"
        if delta < 86400:
            return f"played {int(delta // 3600)} h ago"
        if delta < 7 * 86400:
            return f"played {int(delta // 86400)} d ago"
        return "played " + time.strftime("%d %b", time.localtime(inst.last_played))

    def installed_runtimes(self) -> list[tuple[str, bool]]:
        """``[("Java 8", True), ...]`` for the menu's runtime strip.

        Cheap: :func:`launcher.java.is_installed` is one stat per component, so this can be
        called on every repaint without caching.
        """
        from .. import java as java_mod
        out = []
        for component, major in sorted(java_mod.COMPONENT_MAJOR.items(),
                                       key=lambda kv: kv[1]):
            if component.endswith("-snapshot") or component == "java-runtime-beta":
                continue
            out.append((f"Java {major}", java_mod.is_installed(self.layout, component)))
        return out

    def account_display(self) -> tuple[str, str, str]:
        """``(name, subtitle, subtitle colour)`` for the account chip."""
        if self.dev_account is not None:
            return self.dev_account.name, "dev account · local only", theme.WARN
        entry = self.accounts.active
        if entry is None:
            return "Not signed in", "click to add an account", theme.DIM
        return entry.name, "Microsoft account", theme.OK

    def account_skin_head(self, size: int = 28) -> tk.PhotoImage | None:
        """The player's real skin head, or None if there is not one to render.

        None is a meaningful answer, not a failure: the caller draws a monogram tile
        instead, which looks deliberate where a flat placeholder bitmap looks broken.
        """
        if self.dev_account is not None or self.accounts.active is None:
            return None
        entry = self.accounts.active
        cached = self._head_cache.get(f"{entry.uuid}:{size}")
        if cached is not None:
            return cached
        if entry.skin_url:
            self._background(self._load_head, entry.uuid, entry.skin_url, size,
                             label="skin")
        return None

    def _load_head(self, uuid: str, url: str, size: int) -> None:
        path = skin.fetch_skin(self.layout, uuid, url)
        if path is None:
            return
        self._on_ui(lambda: self._apply_head(uuid, path, size))

    def _apply_head(self, uuid: str, path: Path, size: int) -> None:
        img = skin.head_image(path, size)
        if img is None:
            return
        self._head_cache[f"{uuid}:{size}"] = img
        if self.screen == "home":
            self.home.redraw()

    # ================================================================== library views
    def refresh_instances(self) -> None:
        for w in self.list_inner.winfo_children():
            w.destroy()
        rows = self.instances.all()
        self._cached_instances = rows
        if not rows:
            tk.Label(self.list_inner, text="No instances yet.\n\nClick + New to make one.",
                     bg=theme.PANEL, fg=theme.FAINT, font=self.f["small"],
                     justify="left").pack(padx=12, pady=20, anchor="w")
            return
        for inst in rows:
            self._instance_row(inst)

    def _instance_row(self, inst: Instance) -> None:
        selected = self.selected is not None and self.selected.slug == inst.slug
        bg = theme.PANEL_SEL if selected else theme.PANEL
        row = tk.Frame(self.list_inner, bg=bg, cursor="hand2")
        row.pack(fill="x", pady=1)

        strip = tk.Frame(row, bg=theme.loader_colour(inst.loader),
                         width=3 if not selected else 4)
        strip.pack(side="left", fill="y")

        inner = tk.Frame(row, bg=bg)
        inner.pack(side="left", fill="x", expand=True, padx=10, pady=9)

        name = tk.Label(inner, text=inst.name, bg=bg,
                        fg=theme.TEXT if selected else theme.TEXT,
                        font=self.f["body"], anchor="w")
        name.pack(fill="x")

        sub = inst.badge
        sub += "   ●  running" if self.is_running(inst.slug) \
            else "   " + self.last_played_text(inst)
        meta = tk.Label(inner, text=sub, bg=bg,
                        fg=theme.OK if self.is_running(inst.slug) else theme.DIM,
                        font=self.f["tiny"], anchor="w")
        meta.pack(fill="x")

        def choose(_e=None, i=inst):
            self.select(i)

        for w in (row, inner, name, meta, strip):
            w.bind("<Button-1>", choose)
            w.bind("<Double-Button-1>", lambda _e: self.play())

    def _select_initial(self) -> None:
        rows = self.instances.all()
        if not rows:
            self.show_empty()
            return
        want = self.settings.last_instance
        self.select(next((i for i in rows if i.slug == want), rows[0]))

    def select(self, inst: Instance) -> None:
        self.selected = inst
        self.settings.last_instance = inst.slug
        self.settings.save(self.layout)
        self.refresh_instances()
        self.show_detail()

    def show_empty(self) -> None:
        self.title_lbl.configure(text="Nothing selected")
        self.hero_rail.configure(bg=theme.BORDER)
        for w in self.badges.winfo_children():
            w.destroy()
        for w in self.info.winfo_children():
            w.destroy()
        tk.Label(self.info, bg=theme.BG, fg=theme.DIM, font=self.f["body"],
                 justify="left", anchor="w",
                 text="Create an instance to get started.\n\n"
                      "Each one gets its own mods, saves and config folder, so a Forge "
                      "1.20.1 install can never contaminate a Fabric 26.2 one.").pack(
            anchor="w")
        self.play_btn.set_enabled(False)
        self.join_btn.set_enabled(False)

    def show_detail(self) -> None:
        inst = self.selected
        if inst is None:
            return self.show_empty()
        tint = theme.loader_colour(inst.loader)
        self.hero_rail.configure(bg=tint)
        self.title_lbl.configure(text=inst.name)

        for w in self.badges.winfo_children():
            w.destroy()
        theme.badge(self.badges, inst.mc_version or inst.version_id, theme.TEXT,
                    self.f["tiny"]).pack(side="left", padx=(0, 6))
        theme.badge(self.badges, inst.loader.title(), tint,
                    self.f["tiny"]).pack(side="left", padx=(0, 6))
        theme.badge(self.badges, f"{inst.memory_mb} MB", theme.DIM,
                    self.f["tiny"]).pack(side="left", padx=(0, 6))
        running = self.is_running(inst.slug)
        if running:
            theme.badge(self.badges, "running", theme.OK,
                        self.f["tiny"]).pack(side="left", padx=(0, 6))

        for w in self.info.winfo_children():
            w.destroy()
        mods = sorted(p.name for p in inst.mods_dir.glob("*.jar")) \
            if inst.mods_dir.is_dir() else []
        worlds = sorted(p.name for p in inst.saves_dir.iterdir()) \
            if inst.saves_dir.is_dir() else []

        self._info_block("Game directory", str(inst.dir))
        self._info_block("Version id", inst.version_id)
        if inst.quick_play_server:
            self._info_block("Quick play server", inst.quick_play_server)
        self._info_block(f"Mods ({len(mods)})",
                         "\n".join(mods) if mods else "none in this instance")
        self._info_block(f"Worlds ({len(worlds)})",
                         ", ".join(worlds) if worlds else "none yet")

        can_play = not self.busy and not running
        self.play_btn.set_enabled(can_play)
        self.join_btn.set_enabled(can_play and bool(inst.quick_play_server))
        self.play_btn.configure(text="  Playing  " if running else "  ▶   Play  ")

    def _info_block(self, label: str, value: str) -> None:
        row = tk.Frame(self.info, bg=theme.BG)
        row.pack(fill="x", pady=(0, 10), anchor="w")
        tk.Label(row, text=ck.spaced(label.upper()), bg=theme.BG, fg=theme.FAINT,
                 font=self.f["tiny"], anchor="w", width=28).pack(side="left", anchor="n")
        tk.Label(row, text=value, bg=theme.BG, fg=theme.DIM, font=self.f["small"],
                 anchor="w", justify="left").pack(side="left", anchor="w")

    # ================================================================== accounts
    def refresh_account(self) -> None:
        name, sub, _ = self.account_display()
        try:
            self.lib_account.configure(text=f"{name}  ·  {sub}")
        except tk.TclError:
            pass
        if self.screen == "home":
            self.home.redraw()

    def current_account(self) -> Account:
        if self.dev_account is not None:
            return self.dev_account
        return self.accounts.resolve()

    def open_accounts(self) -> None:
        dlg = SignInDialog(self.root, self)
        self.root.wait_window(dlg.top)
        self.refresh_account()

    # ================================================================== actions
    def create_instance(self) -> None:
        dlg = CreateInstanceDialog(self.root, self)
        self.root.wait_window(dlg.top)
        if dlg.result is None:
            return
        self._background(self._do_create, dlg.result, label="create")

    def _do_create(self, spec: dict) -> None:
        progress = self._progress()
        version_id = install_loader(self.layout, spec["loader"], spec["mc_version"],
                                    loader_version=spec["loader_version"] or None,
                                    progress=progress, cancel=self.cancel)
        inst = self.instances.create(
            spec["name"], version_id, mc_version=spec["mc_version"],
            loader=spec["loader"], loader_version=spec["loader_version"],
            memory_mb=spec["memory_mb"])
        if spec.get("server"):
            inst.quick_play_server = spec["server"]
            inst.save()
            try:
                nbt.add_server(inst.dir / "servers.dat", spec["name"], spec["server"])
            except Exception as exc:  # a bad servers.dat must never block a launch
                log.warning("could not pre-seed servers.dat: %s", exc)
        self._on_ui(lambda: self.open_library(inst))

    def edit_instance(self) -> None:
        if self.selected is None:
            return
        dlg = EditInstanceDialog(self.root, self, self.selected)
        self.root.wait_window(dlg.top)
        self.selected = self.instances.get(self.selected.slug)
        self.refresh_current_screen()

    def duplicate_instance(self) -> None:
        if self.selected is None:
            return
        clone = self.instances.duplicate(self.selected, f"{self.selected.name} copy")
        self.refresh_instances()
        self.select(clone)

    def delete_instance(self) -> None:
        inst = self.selected
        if inst is None:
            return
        if self.is_running(inst.slug):
            messagebox.showwarning(
                "Still running",
                "That instance is running. Close the game first -- Windows holds its "
                "files open and deleting them now would fail halfway through.")
            return
        worlds = list(inst.saves_dir.iterdir()) if inst.saves_dir.is_dir() else []
        if not messagebox.askyesno(
                "Delete instance",
                f"Delete '{inst.name}'?\n\nIts mods and config go with it."):
            return
        delete_saves = False
        if worlds:
            delete_saves = messagebox.askyesno(
                "Delete worlds too?",
                f"'{inst.name}' has {len(worlds)} world(s).\n\n"
                "Yes  = delete the worlds as well.\n"
                "No   = keep them (moved to a _rescued-saves folder).")
        self.instances.delete(inst, delete_saves=delete_saves)
        self.selected = None
        self.refresh_instances()
        self._select_initial()

    def export_instance(self) -> None:
        if self.selected is None:
            return
        dest = filedialog.asksaveasfilename(
            title="Export instance", defaultextension=".zip",
            initialfile=f"{self.selected.slug}.zip",
            filetypes=[("Instance archive", "*.zip")])
        if not dest:
            return
        include = messagebox.askyesno("Include worlds?",
                                      "Include the saves folder in the export?")
        inst = self.selected
        self._background(lambda: self.instances.export_zip(
            inst, Path(dest), include_saves=include), label="export")

    def open_sub(self, sub: str) -> None:
        if self.selected is None:
            return
        target = self.selected.dir / sub if sub else self.selected.dir
        target.mkdir(parents=True, exist_ok=True)
        self._open_folder(target)

    def open_data_folder(self) -> None:
        self._open_folder(self.layout.data_root)

    def _open_folder(self, path: Path) -> None:
        try:
            if sys.platform == "win32":
                os.startfile(str(path))  # noqa: S606 - a folder, by design
            elif sys.platform == "darwin":
                subprocess.Popen(["open", str(path)])
            else:
                subprocess.Popen(["xdg-open", str(path)])
        except OSError as exc:
            messagebox.showerror("Could not open folder", str(exc))

    def open_settings(self) -> None:
        dlg = SettingsDialog(self.root, self)
        self.root.wait_window(dlg.top)
        self.settings = Settings.load(self.layout)
        self.accounts.client_id = self.settings.effective_client_id

    # ================================================================== launching
    def play(self, inst: Instance | None = None, server: str | None = None) -> None:
        inst = inst or self.selected
        if inst is None or self.busy:
            return
        if self.is_running(inst.slug):
            proc = self.running[inst.slug]
            messagebox.showinfo("Already running",
                                f"'{inst.name}' is already running (pid {proc.pid}).")
            return
        try:
            account = self.current_account()
        except AuthError as exc:
            messagebox.showerror("Sign in first", str(exc))
            self.open_accounts()
            return
        self.selected = inst
        self._background(self._do_launch, inst, account, server, label="launch")

    def play_join(self) -> None:
        if self.selected is not None:
            self.play(self.selected, self.selected.quick_play_server or None)

    def _do_launch(self, inst: Instance, account: Account, server: str | None) -> None:
        progress = self._progress()
        self.console_write(f"--- launching {inst.name} as {account.name} ---", "launcher")
        plan = prepare(self.layout, inst, account, settings=self.settings,
                       progress=progress, cancel=self.cancel,
                       quick_play_server=server)
        proc = spawn(plan)
        self.running[inst.slug] = proc
        proc.listen(lambda line: self._on_ui(lambda: self.console_write(line)))
        proc.on_exit.append(
            lambda code: self._on_ui(lambda: self._on_game_exit(inst.slug, code)))
        self._on_ui(lambda: (self.set_status(f"{inst.name} running (pid {proc.pid})"),
                             self.refresh_current_screen()))
        if self.settings.close_on_launch:
            self._on_ui(lambda: self.root.after(3000, self.root.destroy))

    def _on_game_exit(self, slug: str, code: int) -> None:
        proc = self.running.get(slug)
        self.console_write(f"--- game exited with code {code} ---", "launcher")
        if proc is not None and code != 0:
            for line in proc.tail_lines(40):
                self.console_write(line, "err" if "Exception" in line else None)
            messagebox.showerror("The game stopped", proc.diagnose()[:1500])
        elif proc is not None:
            verdict = proc.diagnose()
            if "immediately" in verdict:
                self.console_write(verdict, "warn")
        self.set_status("Ready")
        self.refresh_current_screen()

    # ================================================================== threading
    def _progress(self) -> net.Progress:
        p = net.Progress()
        p.listen(lambda pr: self._on_ui(lambda: self._render_progress(pr)))
        return p

    def _render_progress(self, pr: net.Progress) -> None:
        try:
            self.progress.set(pr.fraction)
        except tk.TclError:
            pass
        detail = pr.detail[:60]
        pct = int(pr.fraction * 100)
        self.set_status(f"{pr.describe()}  {pct}%   {detail}" if detail
                        else f"{pr.describe()}  {pct}%")

    def _background(self, fn, *args, label: str = "work") -> None:
        if self.busy and label != "skin":
            messagebox.showinfo("Busy", "Something is already running. Let it finish.")
            return
        if label != "skin":
            self.busy = True
            self.cancel = net.CancelToken()
            self.cancel_btn.pack(side="right", padx=(0, 8))
            self.refresh_current_screen()

        def run():
            try:
                fn(*args)
            except net.Cancelled:
                self._on_ui(lambda: self.set_status("Cancelled"))
            except (LaunchError, AuthError, net.NetError, RuntimeError, OSError) as exc:
                message = str(exc)
                log.error("%s failed: %s", label, message)
                if label != "skin":
                    self._on_ui(lambda: self._report_error(label, message))
            finally:
                if label != "skin":
                    self._on_ui(self._work_done)

        threading.Thread(target=run, name=f"bg-{label}", daemon=True).start()

    def _work_done(self) -> None:
        self.busy = False
        self.cancel = None
        try:
            self.cancel_btn.pack_forget()
            self.progress.set(0.0)
        except tk.TclError:
            pass
        self.refresh_current_screen()

    def _report_error(self, label: str, message: str) -> None:
        self.console_write(f"--- {label} failed ---", "err")
        for line in message.splitlines():
            self.console_write(line, "err")
        if self.screen == "home":
            self.open_library()
        self.show_console()
        messagebox.showerror(f"Could not {label}", message[:1500])
        self.set_status("Failed")

    def cancel_work(self) -> None:
        if self.cancel is not None:
            self.cancel.cancel()
            self.set_status("Cancelling...")

    def _on_ui(self, fn) -> None:
        self._ui_queue.put(fn)

    def _on_ui_thread(self) -> bool:
        return threading.current_thread() is self._main_thread

    def _pump(self) -> None:
        self.watchdog.beat()
        for _ in range(200):
            try:
                fn = self._ui_queue.get_nowait()
            except queue.Empty:
                break
            try:
                fn()
            except Exception:
                log.debug("ui callback raised", exc_info=True)
        self.root.after(60, self._pump)

    # ================================================================== console
    #
    # Tk is not thread-safe, and these two are the methods a worker thread most naturally
    # reaches for mid-job. Touching a widget off the main thread does not raise -- it
    # deadlocks, silently, with the window still painted and apparently alive. So both
    # self-marshal rather than trusting every caller to remember.
    def set_status(self, text: str) -> None:
        self.status_text = text
        if not self._on_ui_thread():
            self._on_ui(lambda: self.set_status(text))
            return
        try:
            self.status.configure(text=text)
        except tk.TclError:
            pass
        self.home.set_status(text)

    def console_write(self, line: str, tag: str | None = None) -> None:
        if not self._on_ui_thread():
            self._on_ui(lambda: self.console_write(line, tag))
            return
        if tag is None:
            low = line.lower()
            tag = "err" if "/error" in low or "exception" in low else (
                "warn" if "/warn" in low else "plain")
        self.console.configure(state="normal")
        self.console.insert("end", logs.redact(line) + "\n", tag)
        # Keep the buffer bounded; a modded launch emits tens of thousands of lines.
        if int(self.console.index("end-1c").split(".")[0]) > 4000:
            self.console.delete("1.0", "1000.0")
        self.console.see("end")
        self.console.configure(state="disabled")

    def toggle_console(self) -> None:
        if self.console_visible:
            self.console_frame.pack_forget()
        else:
            self.console_frame.pack(fill="both", expand=True, padx=16, pady=(0, 12))
        self.console_visible = not self.console_visible

    def show_console(self) -> None:
        if not self.console_visible:
            self.toggle_console()

    def _show_crash(self, text: str) -> None:
        """Shown once per session when a Tk callback blows up, so it is not invisible."""
        last = text.strip().splitlines()[-1] if text.strip() else ""
        messagebox.showerror(
            "Something went wrong",
            "The launcher hit an internal error. The details were written to:"
            + "\n\n" + str(self.layout.logs / "launcher.log")
            + "\n\n" + last)

    # ================================================================== lifecycle
    def on_close(self) -> None:
        live = [s for s, p in self.running.items() if p.running]
        if live and not messagebox.askyesno(
                "Close the launcher?",
                f"{len(live)} game(s) still running.\n\n"
                "They will keep running -- the launcher does not own them. Close anyway?"):
            return
        self.settings.save(self.layout)
        self.watchdog.stop()
        log.info("launcher closed cleanly")
        self.root.destroy()

    def run(self) -> int:
        self.root.mainloop()
        return 0


def main(layout: Layout | None = None) -> int:
    layout = layout or Layout()
    logs.setup(layout.logs)
    app = App(layout)
    if dev_mode_enabled():
        app.set_status("Dev mode: a local dev account is available for local servers.")
    return app.run()


if __name__ == "__main__":
    raise SystemExit(main())
