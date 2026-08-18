"""Modal dialogs: create, edit, settings, sign in.

The create dialog defaults to **releases only** deliberately. The unfiltered manifest is
around 900 entries going back to 2009 alphas; showing all of it by default makes the list
unusable, so snapshots and old betas/alphas are opt-in checkboxes.

The loader-version dropdown is populated from the chosen Minecraft version, so it only ever
offers builds that can actually work with it -- picking an incompatible pair should not be
possible from the UI at all.
"""
from __future__ import annotations

import threading
import tkinter as tk
from tkinter import messagebox

from .. import logs, net, versions
from ..accounts import AuthError, LocalAccount, dev_mode_enabled
from ..accounts.local import DEV_ENV_FLAG
from ..instances import Instance
from ..loaders import LOADERS, loader_versions
from ..settings import Settings
from . import theme

log = logs.get("ui.dialogs")


class Dialog:
    """Shared modal plumbing: centred, transient, escape-to-close."""

    WIDTH = 560
    HEIGHT = 460
    TITLE = "Dialog"

    def __init__(self, parent: tk.Misc, app) -> None:
        self.app = app
        self.f = app.f
        self.result = None
        self.top = tk.Toplevel(parent)
        self.top.title(self.TITLE)
        self.top.configure(bg=theme.BG)
        self.top.transient(parent)
        self.top.grab_set()
        self.top.resizable(False, False)
        from .app import _dark_titlebar
        _dark_titlebar(self.top)
        parent.update_idletasks()
        x = parent.winfo_rootx() + (parent.winfo_width() - self.WIDTH) // 2
        y = parent.winfo_rooty() + (parent.winfo_height() - self.HEIGHT) // 3
        self.top.geometry(f"{self.WIDTH}x{self.HEIGHT}+{max(0, x)}+{max(0, y)}")
        self.top.bind("<Escape>", lambda _e: self.close())
        self.body = tk.Frame(self.top, bg=theme.BG)
        self.body.pack(fill="both", expand=True, padx=22, pady=18)

    def close(self) -> None:
        try:
            self.top.grab_release()
        except tk.TclError:
            pass
        self.top.destroy()

    def heading(self, text: str, sub: str = "") -> None:
        tk.Label(self.body, text=text, bg=theme.BG, fg=theme.TEXT, font=self.f["h2"],
                 anchor="w").pack(fill="x")
        if sub:
            tk.Label(self.body, text=sub, bg=theme.BG, fg=theme.DIM,
                     font=self.f["small"], anchor="w", justify="left",
                     wraplength=self.WIDTH - 60).pack(fill="x", pady=(2, 12))
        else:
            tk.Frame(self.body, bg=theme.BG, height=10).pack()

    def field(self, label: str, value: str = "", width: int = 30) -> tk.Entry:
        row = tk.Frame(self.body, bg=theme.BG)
        row.pack(fill="x", pady=4)
        tk.Label(row, text=label, bg=theme.BG, fg=theme.DIM, font=self.f["small"],
                 width=18, anchor="w").pack(side="left")
        entry = tk.Entry(row, bg=theme.PANEL_HI, fg=theme.TEXT, font=self.f["body"],
                         insertbackground=theme.TEXT, relief="flat", width=width)
        entry.insert(0, value)
        entry.pack(side="left", fill="x", expand=True, ipady=4)
        return entry

    def buttons(self, ok_label: str, on_ok) -> None:
        row = tk.Frame(self.body, bg=theme.BG)
        row.pack(side="bottom", fill="x", pady=(14, 0))
        theme.Button(row, ok_label, on_ok, kind="primary",
                     font=self.f["body"]).pack(side="right")
        theme.Button(row, "Cancel", self.close, kind="ghost",
                     font=self.f["body"]).pack(side="right", padx=8)


# ---------------------------------------------------------------------------------

class CreateInstanceDialog(Dialog):
    TITLE = "New instance"
    WIDTH = 620
    HEIGHT = 560

    def __init__(self, parent, app):
        super().__init__(parent, app)
        self.heading("New instance",
                     "It gets its own mods, saves and config folder. Nothing here can "
                     "reach any other install.")

        self.name_entry = self.field("Name", "")
        self.memory_entry = self.field("Memory (MB)",
                                       str(app.settings.default_memory_mb), width=10)
        self.server_entry = self.field("Server (optional)", "")

        filt = tk.Frame(self.body, bg=theme.BG)
        filt.pack(fill="x", pady=(12, 4))
        tk.Label(filt, text="Minecraft version", bg=theme.BG, fg=theme.DIM,
                 font=self.f["small"], width=18, anchor="w").pack(side="left")
        self.show_snapshots = tk.BooleanVar(value=app.settings.show_snapshots)
        self.show_old = tk.BooleanVar(value=app.settings.show_old_versions)
        for text, var in (("snapshots", self.show_snapshots),
                          ("old beta/alpha", self.show_old)):
            tk.Checkbutton(filt, text=text, variable=var, command=self._refill_versions,
                           bg=theme.BG, fg=theme.DIM, font=self.f["tiny"],
                           selectcolor=theme.PANEL_HI, activebackground=theme.BG,
                           activeforeground=theme.TEXT, bd=0,
                           highlightthickness=0).pack(side="left", padx=4)

        listrow = tk.Frame(self.body, bg=theme.BG)
        listrow.pack(fill="both", expand=True, pady=(0, 6))
        self.version_list = tk.Listbox(
            listrow, bg=theme.PANEL_HI, fg=theme.TEXT, font=self.f["mono"], height=9,
            selectbackground=theme.ACCENT, selectforeground="#1a1206", bd=0,
            highlightthickness=0, activestyle="none", exportselection=False)
        bar = tk.Scrollbar(listrow, command=self.version_list.yview)
        self.version_list.configure(yscrollcommand=bar.set)
        self.version_list.pack(side="left", fill="both", expand=True)
        bar.pack(side="right", fill="y")
        self.version_list.bind("<<ListboxSelect>>", lambda _e: self._refill_loaders())

        loader_row = tk.Frame(self.body, bg=theme.BG)
        loader_row.pack(fill="x", pady=(6, 0))
        tk.Label(loader_row, text="Mod loader", bg=theme.BG, fg=theme.DIM,
                 font=self.f["small"], width=18, anchor="w").pack(side="left")
        self.loader_var = tk.StringVar(value="vanilla")
        self.loader_menu = tk.OptionMenu(loader_row, self.loader_var, *LOADERS,
                                         command=lambda _v: self._refill_loaders())
        self._style_menu(self.loader_menu)
        self.loader_menu.pack(side="left")

        self.loader_version_var = tk.StringVar(value="latest")
        self.loader_version_menu = tk.OptionMenu(loader_row, self.loader_version_var,
                                                 "latest")
        self._style_menu(self.loader_version_menu)
        self.loader_version_menu.pack(side="left", padx=8)

        self.note = tk.Label(self.body, text="", bg=theme.BG, fg=theme.DIM,
                             font=self.f["tiny"], anchor="w", justify="left",
                             wraplength=self.WIDTH - 60)
        self.note.pack(fill="x", pady=(6, 0))

        self.buttons("Create", self._submit)
        self.manifest = None
        self._load_manifest()

    def _style_menu(self, menu: tk.OptionMenu) -> None:
        menu.configure(bg=theme.PANEL_HI, fg=theme.TEXT, font=self.f["small"],
                       activebackground=theme.PANEL_SEL, activeforeground=theme.TEXT,
                       relief="flat", highlightthickness=0, bd=0, width=10)
        menu["menu"].configure(bg=theme.PANEL_HI, fg=theme.TEXT, font=self.f["small"],
                               activebackground=theme.ACCENT, bd=0)

    def _load_manifest(self):
        self.note.configure(text="Loading version list...")

        def work():
            try:
                manifest = versions.load_manifest(self.app.layout)
            except net.NetError as exc:
                self.app._on_ui(lambda: self.note.configure(
                    text=f"Could not load the version list: {exc}", fg=theme.ERR))
                return
            self.app._on_ui(lambda: self._manifest_ready(manifest))

        threading.Thread(target=work, daemon=True, name="manifest").start()

    def _manifest_ready(self, manifest):
        self.manifest = manifest
        self.note.configure(text="", fg=theme.DIM)
        self._refill_versions()

    def _refill_versions(self):
        if self.manifest is None:
            return
        types = ["release"]
        if self.show_snapshots.get():
            types.append("snapshot")
        if self.show_old.get():
            types += ["old_beta", "old_alpha"]
        entries = self.manifest.filtered(types)
        self.version_list.delete(0, "end")
        for v in entries:
            marker = "  " if v.type == "release" else " ·"
            self.version_list.insert("end", f"{marker}{v.id:<22}{v.type}")
        self._entries = entries
        if entries:
            self.version_list.selection_set(0)
            self.version_list.see(0)
            self._refill_loaders()

    def _selected_version(self) -> str | None:
        sel = self.version_list.curselection()
        if not sel or not getattr(self, "_entries", None):
            return None
        return self._entries[sel[0]].id

    def _refill_loaders(self):
        loader = self.loader_var.get()
        mc = self._selected_version()
        menu = self.loader_version_menu["menu"]
        menu.delete(0, "end")
        if loader == "vanilla" or not mc:
            self.loader_version_var.set("-")
            menu.add_command(label="-",
                             command=lambda: self.loader_version_var.set("-"))
            self.note.configure(text="", fg=theme.DIM)
            return
        self.loader_version_var.set("loading...")
        self.note.configure(text=f"Looking up {loader} builds for {mc}...", fg=theme.DIM)

        def work():
            found = loader_versions(loader, mc)
            self.app._on_ui(lambda: self._loaders_ready(loader, mc, found))

        threading.Thread(target=work, daemon=True, name="loader-versions").start()

    def _loaders_ready(self, loader: str, mc: str, found: list[str]):
        if loader != self.loader_var.get() or mc != self._selected_version():
            return  # the user moved on while we were looking
        menu = self.loader_version_menu["menu"]
        menu.delete(0, "end")
        if not found:
            self.loader_version_var.set("none")
            menu.add_command(label="none",
                             command=lambda: self.loader_version_var.set("none"))
            self.note.configure(
                text=f"{loader.title()} has no build for Minecraft {mc}. "
                     "Pick another version or loader.", fg=theme.WARN)
            return
        for v in found[:60]:
            menu.add_command(label=v,
                             command=lambda val=v: self.loader_version_var.set(val))
        self.loader_version_var.set(found[0])
        self.note.configure(text=f"{len(found)} {loader} build(s) for {mc}; "
                                 f"newest first.", fg=theme.DIM)

    def _submit(self):
        name = self.name_entry.get().strip()
        mc = self._selected_version()
        loader = self.loader_var.get()
        loader_version = self.loader_version_var.get()
        if not name:
            return messagebox.showwarning("Name needed", "Give the instance a name.",
                                          parent=self.top)
        if not mc:
            return messagebox.showwarning("Version needed",
                                          "Pick a Minecraft version.", parent=self.top)
        if loader != "vanilla" and loader_version in ("none", "loading...", "-"):
            return messagebox.showwarning(
                "Loader version needed",
                f"No usable {loader} build is selected for {mc}.", parent=self.top)
        try:
            memory = int(self.memory_entry.get().strip() or "4096")
        except ValueError:
            return messagebox.showwarning("Memory", "Memory must be a number of MB.",
                                          parent=self.top)
        self.result = {
            "name": name, "mc_version": mc, "loader": loader,
            "loader_version": "" if loader == "vanilla" else loader_version,
            "memory_mb": max(512, memory),
            "server": self.server_entry.get().strip(),
        }
        self.app.settings.show_snapshots = self.show_snapshots.get()
        self.app.settings.show_old_versions = self.show_old.get()
        self.app.settings.save(self.app.layout)
        self.close()


# ---------------------------------------------------------------------------------

class EditInstanceDialog(Dialog):
    TITLE = "Edit instance"
    HEIGHT = 430

    def __init__(self, parent, app, inst: Instance):
        super().__init__(parent, app)
        self.inst = inst
        self.heading(f"Edit {inst.name}", f"Version {inst.version_id}")
        self.name_entry = self.field("Name", inst.name)
        self.memory_entry = self.field("Memory (MB)", str(inst.memory_mb), width=10)
        self.java_entry = self.field("Java override", inst.java_override)
        self.width_entry = self.field("Window width", str(inst.width or ""), width=10)
        self.height_entry = self.field("Window height", str(inst.height or ""), width=10)
        self.server_entry = self.field("Quick play server", inst.quick_play_server)
        self.jvm_entry = self.field("Extra JVM args", " ".join(inst.extra_jvm_args))
        tk.Label(self.body,
                 text="Leave the Java override blank to let the launcher pick and "
                      "download the right one. Extra JVM args are off by default on "
                      "purpose -- forum GC flags usually make things worse.",
                 bg=theme.BG, fg=theme.FAINT, font=self.f["tiny"], anchor="w",
                 justify="left", wraplength=self.WIDTH - 60).pack(fill="x", pady=(10, 0))
        self.buttons("Save", self._submit)

    def _submit(self):
        inst = self.inst
        inst.name = self.name_entry.get().strip() or inst.name
        try:
            inst.memory_mb = max(512, int(self.memory_entry.get().strip() or "4096"))
            inst.width = int(self.width_entry.get().strip() or "0")
            inst.height = int(self.height_entry.get().strip() or "0")
        except ValueError:
            return messagebox.showwarning("Numbers", "Memory and size must be numbers.",
                                          parent=self.top)
        inst.java_override = self.java_entry.get().strip()
        inst.quick_play_server = self.server_entry.get().strip()
        inst.extra_jvm_args = self.jvm_entry.get().split()
        inst.save()
        self.close()


# ---------------------------------------------------------------------------------

class SettingsDialog(Dialog):
    TITLE = "Settings"
    HEIGHT = 420

    def __init__(self, parent, app):
        super().__init__(parent, app)
        s = app.settings
        self.heading("Settings", f"Data root: {app.layout.data_root}")
        self.memory_entry = self.field("Default memory (MB)",
                                       str(s.default_memory_mb), width=10)
        self.conc_entry = self.field("Download concurrency",
                                     str(s.download_concurrency), width=10)
        self.java_entry = self.field("Default Java override", s.java_override)
        self.client_entry = self.field("Azure client ID", s.client_id)
        self.close_var = tk.BooleanVar(value=s.close_on_launch)
        tk.Checkbutton(self.body, text="Close the launcher after the game starts",
                       variable=self.close_var, bg=theme.BG, fg=theme.DIM,
                       font=self.f["small"], selectcolor=theme.PANEL_HI,
                       activebackground=theme.BG, activeforeground=theme.TEXT,
                       bd=0, highlightthickness=0, anchor="w").pack(fill="x", pady=8)
        tk.Label(self.body,
                 text="The Azure client ID comes from your own app registration. Until "
                      "Microsoft approves it for the Minecraft API, sign-in will fail "
                      "with a 403 -- that is an approval status, not a bug. See the "
                      "README's first section.",
                 bg=theme.BG, fg=theme.FAINT, font=self.f["tiny"], anchor="w",
                 justify="left", wraplength=self.WIDTH - 60).pack(fill="x")
        self.buttons("Save", self._submit)

    def _submit(self):
        s = self.app.settings
        try:
            s.default_memory_mb = max(512, int(self.memory_entry.get().strip()))
            s.download_concurrency = max(1, min(32, int(self.conc_entry.get().strip())))
        except ValueError:
            return messagebox.showwarning("Numbers", "Those must be numbers.",
                                          parent=self.top)
        s.java_override = self.java_entry.get().strip()
        s.client_id = self.client_entry.get().strip()
        s.close_on_launch = bool(self.close_var.get())
        s.save(self.app.layout)
        self.close()


# ---------------------------------------------------------------------------------

class SignInDialog(Dialog):
    TITLE = "Accounts"
    WIDTH = 600
    HEIGHT = 470

    def __init__(self, parent, app):
        super().__init__(parent, app)
        self.heading("Accounts",
                     "Multiplayer on a real server needs a real Microsoft account -- the "
                     "server asks Mojang to confirm the session, and nothing else "
                     "satisfies that.")

        self.rows = tk.Frame(self.body, bg=theme.BG)
        self.rows.pack(fill="x")
        self._render_accounts()

        theme.hline(self.body).pack(fill="x", pady=14)

        self.code_frame = tk.Frame(self.body, bg=theme.PANEL_HI)
        self.code_label = tk.Label(self.code_frame, text="", bg=theme.PANEL_HI,
                                   fg=theme.ACCENT, font=self.f["code"])
        self.code_label.pack(pady=(10, 2))
        self.code_hint = tk.Label(self.code_frame, text="", bg=theme.PANEL_HI,
                                  fg=theme.DIM, font=self.f["small"], wraplength=500,
                                  justify="center")
        self.code_hint.pack(pady=(0, 10))

        actions = tk.Frame(self.body, bg=theme.BG)
        actions.pack(fill="x")
        theme.Button(actions, "Sign in with Microsoft", self._sign_in, kind="primary",
                     font=self.f["body"]).pack(side="left")

        if dev_mode_enabled():
            theme.Button(actions, "Use a dev account", self._dev_account, kind="normal",
                         font=self.f["small"]).pack(side="left", padx=8)

        self.status = tk.Label(self.body, text="", bg=theme.BG, fg=theme.DIM,
                               font=self.f["small"], anchor="w", justify="left",
                               wraplength=self.WIDTH - 60)
        self.status.pack(fill="x", pady=(12, 0))
        if not app.settings.client_id:
            self.status.configure(
                text="No Azure client ID is set yet (Settings). Without one, real "
                     "sign-in cannot start.", fg=theme.WARN)

        theme.Button(self.body, "Close", self.close, kind="ghost",
                     font=self.f["body"]).pack(side="bottom", anchor="e")

    def _render_accounts(self):
        for w in self.rows.winfo_children():
            w.destroy()
        summaries = self.app.accounts.summaries()
        if self.app.dev_account is not None:
            self._account_row(f"{self.app.dev_account.name} (dev)",
                              "local servers only, cannot join online-mode servers",
                              active=True, on_remove=self._clear_dev)
        if not summaries and self.app.dev_account is None:
            tk.Label(self.rows, text="No accounts signed in.", bg=theme.BG,
                     fg=theme.FAINT, font=self.f["small"], anchor="w").pack(fill="x")
        for s in summaries:
            self._account_row(s["name"], "Microsoft account", active=s["active"],
                              on_select=lambda u=s["uuid"]: self._activate(u),
                              on_remove=lambda u=s["uuid"]: self._remove(u))

    def _account_row(self, name: str, sub: str, *, active: bool,
                     on_select=None, on_remove=None):
        bg = theme.PANEL_SEL if active else theme.PANEL
        row = tk.Frame(self.rows, bg=bg)
        row.pack(fill="x", pady=2)
        inner = tk.Frame(row, bg=bg)
        inner.pack(side="left", padx=10, pady=6)
        tk.Label(inner, text=name, bg=bg, fg=theme.TEXT, font=self.f["body"],
                 anchor="w").pack(anchor="w")
        tk.Label(inner, text=sub, bg=bg, fg=theme.DIM, font=self.f["tiny"],
                 anchor="w").pack(anchor="w")
        if on_remove:
            theme.Button(row, "Sign out", on_remove, kind="ghost",
                         font=self.f["tiny"], padx=6, pady=2).pack(side="right", padx=8)
        if on_select and not active:
            theme.Button(row, "Use", on_select, kind="ghost", font=self.f["tiny"],
                         padx=6, pady=2).pack(side="right")

    def _activate(self, uuid: str):
        self.app.dev_account = None
        self.app.accounts.set_active(uuid)
        self._render_accounts()
        self.app.refresh_account()

    def _remove(self, uuid: str):
        self.app.accounts.remove(uuid)
        self._render_accounts()
        self.app.refresh_account()

    def _clear_dev(self):
        self.app.dev_account = None
        self._render_accounts()
        self.app.refresh_account()

    def _dev_account(self):
        from tkinter import simpledialog
        name = simpledialog.askstring(
            "Dev account", "Username for local testing (1-16 characters):",
            parent=self.top)
        if not name:
            return
        try:
            self.app.dev_account = LocalAccount(name)
        except AuthError as exc:
            return messagebox.showerror("Dev account", str(exc), parent=self.top)
        self._render_accounts()
        self.app.refresh_account()
        self.status.configure(
            text="Dev account active. It cannot join an online-mode server -- only local "
                 "ones with online-mode=false.", fg=theme.WARN)

    def _sign_in(self):
        client_id = self.app.settings.effective_client_id
        if not client_id:
            return messagebox.showwarning(
                "No client ID",
                "Set your Azure application (client) ID in Settings first.\n\n"
                "See the README's first section for the ten-minute registration.",
                parent=self.top)
        self.status.configure(text="Asking Microsoft for a sign-in code...",
                              fg=theme.DIM)

        def on_code(code):
            self.app._on_ui(lambda: self._show_code(code))

        def work():
            try:
                self.app.accounts.client_id = client_id
                self.app.accounts.add_microsoft(on_code=on_code)
            except AuthError as exc:
                self.app._on_ui(lambda: self._failed(str(exc)))
            except net.NetError as exc:
                self.app._on_ui(lambda: self._failed(str(exc)))
            else:
                self.app._on_ui(self._succeeded)

        threading.Thread(target=work, daemon=True, name="sign-in").start()

    def _show_code(self, code):
        self.code_frame.pack(fill="x", pady=(0, 10))
        self.code_label.configure(text=code.user_code)
        self.code_hint.configure(
            text=f"Enter this code at {code.verification_uri} "
                 f"(opening in your browser now). Waiting for you to finish...")
        self.code_label.bind("<Button-1>", lambda _e: self._copy(code.user_code))
        code.open_browser()
        self.status.configure(text="Click the code to copy it.", fg=theme.DIM)

    def _copy(self, text: str):
        self.top.clipboard_clear()
        self.top.clipboard_append(text)
        self.status.configure(text="Code copied to the clipboard.", fg=theme.OK)

    def _succeeded(self):
        self.code_frame.pack_forget()
        self.app.dev_account = None
        self._render_accounts()
        self.app.refresh_account()
        self.status.configure(text="Signed in.", fg=theme.OK)

    def _failed(self, message: str):
        self.code_frame.pack_forget()
        self.status.configure(text=message, fg=theme.ERR)
        log.warning("sign-in failed: %s", message)
