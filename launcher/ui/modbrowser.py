"""The mod browser: search Modrinth, install into one instance.

Every search is scoped to the instance's exact Minecraft version and loader, and the dialog
says so in its own heading. That is deliberate: the commonest way people break a modded
install is putting a Fabric jar next to a Forge one, and a browser that offers incompatible
results is an invitation to do exactly that.

Searching and installing both happen on worker threads. Results arrive through the app's UI
queue, and a stale response is discarded by comparing a monotonically increasing request id
-- otherwise typing quickly leaves whichever request happened to finish last on screen,
which is rarely the one that matches the box.
"""
from __future__ import annotations

import threading
import tkinter as tk
from tkinter import messagebox

from .. import logs, net
from ..instances import Instance
from ..mods import modrinth as mr
from . import theme
from .dialogs import Dialog

log = logs.get("ui.modbrowser")


class ModBrowserDialog(Dialog):
    TITLE = "Add mods"
    WIDTH = 760
    HEIGHT = 620

    def __init__(self, parent, app, instance: Instance):
        super().__init__(parent, app)
        self.instance = instance
        self.results: list[mr.ModProject] = []
        self.rows: list[tk.Frame] = []
        self._request = 0
        self._search_job: str | None = None
        self._installed = mr.installed_filenames(instance)

        loader = instance.loader.title()
        self.heading(
            f"Add mods to {instance.name}",
            f"Searching Modrinth for {instance.mc_version} · {loader}. Only compatible "
            f"mods are shown, and they install into this instance's own mods folder.")

        bar = tk.Frame(self.body, bg=theme.BG)
        bar.pack(fill="x", pady=(0, 10))
        self.query = tk.Entry(bar, bg=theme.PANEL_HI, fg=theme.TEXT, font=self.f["body"],
                              insertbackground=theme.TEXT, relief="flat")
        self.query.pack(side="left", fill="x", expand=True, ipady=6, padx=(0, 8))
        self.query.bind("<KeyRelease>", self._on_typing)
        self.query.bind("<Return>", lambda _e: self._search_now())
        theme.Button(bar, "Search", self._search_now, kind="primary",
                     font=self.f["small"]).pack(side="left")

        if instance.loader == "vanilla":
            tk.Label(self.body,
                     text="This is a vanilla instance, so almost nothing will be "
                          "compatible. Edit it to use Fabric, Forge, Quilt or NeoForge "
                          "first.",
                     bg=theme.BG, fg=theme.WARN, font=self.f["small"], anchor="w",
                     justify="left", wraplength=self.WIDTH - 60).pack(fill="x",
                                                                      pady=(0, 8))

        self.list_outer, self.list_inner = theme.scrollable(self.body, theme.BG)
        self.list_outer.pack(fill="both", expand=True)

        self.note = tk.Label(self.body, text="", bg=theme.BG, fg=theme.DIM,
                             font=self.f["small"], anchor="w", justify="left",
                             wraplength=self.WIDTH - 60)
        self.note.pack(fill="x", pady=(8, 0))

        theme.Button(self.body, "Done", self.close, kind="normal",
                     font=self.f["body"]).pack(side="bottom", anchor="e", pady=(8, 0))

        self._search_now()

    # ------------------------------------------------------------------ searching
    def _on_typing(self, _event=None) -> None:
        # Debounce: one request per pause, not one per keystroke.
        if self._search_job is not None:
            self.top.after_cancel(self._search_job)
        self._search_job = self.top.after(350, self._search_now)

    def _search_now(self) -> None:
        self._search_job = None
        self._request += 1
        request = self._request
        query = self.query.get().strip()
        self._set_note("Searching Modrinth...", theme.DIM)

        def work():
            try:
                results, total = mr.search(
                    query, mc_version=self.instance.mc_version,
                    loader=self.instance.loader, limit=25)
            except (net.NetError, ValueError) as exc:
                self.app._on_ui(lambda: self._failed(request, str(exc)))
                return
            self.app._on_ui(lambda: self._show(request, results, total))

        threading.Thread(target=work, daemon=True, name="mod-search").start()

    def _failed(self, request: int, message: str) -> None:
        if request != self._request:
            return
        self._set_note(f"Could not reach Modrinth: {message[:140]}", theme.ERR)

    def _show(self, request: int, results: list[mr.ModProject], total: int) -> None:
        if request != self._request:
            return  # a newer search has already been issued; this answer is stale
        self.results = results
        for row in self.rows:
            row.destroy()
        self.rows.clear()

        if not results:
            self._set_note(
                f"Nothing on Modrinth matches that for {self.instance.mc_version} "
                f"{self.instance.loader.title()}.", theme.WARN)
            return

        self._set_note(f"{total} compatible mods. Showing {len(results)}.", theme.DIM)
        for project in results:
            self.rows.append(self._row(project))

    def _row(self, project: mr.ModProject) -> tk.Frame:
        row = tk.Frame(self.list_inner, bg=theme.PANEL)
        # Right margin so the Install button can never end up under the scrollbar, which
        # appears only once the list is long enough to need it.
        row.pack(fill="x", pady=2, padx=(0, 18))

        left = tk.Frame(row, bg=theme.PANEL)
        left.pack(side="left", fill="x", expand=True, padx=12, pady=9)

        title = tk.Frame(left, bg=theme.PANEL)
        title.pack(fill="x")
        tk.Label(title, text=project.title, bg=theme.PANEL, fg=theme.TEXT,
                 font=self.f["h3"], anchor="w").pack(side="left")
        tk.Label(title, text=f"   {project.downloads_short} downloads", bg=theme.PANEL,
                 fg=theme.FAINT, font=self.f["tiny"], anchor="w").pack(side="left")

        tk.Label(left, text=project.description[:110], bg=theme.PANEL, fg=theme.DIM,
                 font=self.f["small"], anchor="w", justify="left").pack(fill="x")

        button = theme.Button(row, "Install", None, kind="primary",
                              font=self.f["small"], padx=14, pady=6)
        button.pack(side="right", padx=12)
        button.set_command(lambda p=project, b=button: self._install(p, b))
        return row

    # ------------------------------------------------------------------ installing
    def _install(self, project: mr.ModProject, button) -> None:
        button.set_enabled(False)
        button.configure(text="...")
        self._set_note(f"Resolving {project.title}...", theme.DIM)

        def work():
            try:
                version = mr.best_version(project.project_id,
                                          mc_version=self.instance.mc_version,
                                          loader=self.instance.loader)
                if version is None:
                    raise mr.ModrinthError(
                        f"{project.title} has no build for {self.instance.mc_version} "
                        f"{self.instance.loader.title()}.")
                result = mr.install(self.instance, version)
            except (net.NetError, mr.ModrinthError, OSError) as exc:
                self.app._on_ui(lambda: self._install_failed(project, button, str(exc)))
                return
            self.app._on_ui(lambda: self._installed_ok(project, button, result))

        threading.Thread(target=work, daemon=True, name="mod-install").start()

    def _installed_ok(self, project, button, result: mr.InstallResult) -> None:
        button.configure(text="Installed" if not result.failed else "Partly")
        extra = len(result.installed) + len(result.skipped) - 1
        note = f"{project.title}: {result.summary}"
        if extra > 0:
            note += f"  (including {extra} required dependenc" \
                    f"{'y' if extra == 1 else 'ies'})"
        self._set_note(note, theme.OK if not result.failed else theme.WARN)
        for name, why in result.failed:
            log.warning("mod install problem: %s -- %s", name, why)
        self.app.refresh_current_screen()

    def _install_failed(self, project, button, message: str) -> None:
        button.set_enabled(True)
        button.configure(text="Install")
        self._set_note(f"{project.title}: {message[:160]}", theme.ERR)

    def _set_note(self, text: str, colour: str) -> None:
        self.note.configure(text=text, fg=colour)
