r"""How a click on Play becomes a process.

The pipeline is six verbs and this module is the last three of them:

    authenticate -> resolve -> download -> **verify -> launch -> supervise**

Two things here are load-bearing on Windows and neither is optional.

**Arguments are an array, never a string.** Install paths contain spaces -- the one this
was written on has two of them. Any code that builds a command line by concatenation splits
``...\Some Folder\...`` at the space, and the launch fails with a message about a missing
main class, which points nowhere near the real cause. Every argument in this file travels as
its own list element from creation to :class:`Popen`.

**The game must outlive the launcher.** The child is spawned detached, in its own process
group, with stdout and stderr redirected to a *file* rather than a pipe. A pipe would die
with the launcher and take the game's writes with it; a file handle is the child's own. The
in-app console tails that file, which gives the same live output and still works when the
launcher is closed mid-session.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable, Sequence

from . import assets as assets_mod
from . import java as java_mod
from . import libraries as lib_mod
from . import logs, net, rules, versions
from .accounts import Account
from .instances import Instance, guard_game_dir
from .paths import APP_NAME, APP_VERSION, Layout, ext, mkdirs
from .rules import Features, OsInfo
from .settings import Settings

log = logs.get("launch")

_PLACEHOLDER = re.compile(r"\$\{([A-Za-z0-9_]+)\}")


class LaunchError(RuntimeError):
    pass


# ---------------------------------------------------------------------------------
# Argument templating
# ---------------------------------------------------------------------------------

def substitute(value: str, table: dict[str, str], *, where: str = "") -> str:
    """Replace every ``${placeholder}``. An unresolved one is an error, not a warning.

    A surviving ``${...}`` shows up as a literal in the window title, or as a crash deep
    inside the game with no mention of the launcher. Failing here names the placeholder.
    """
    missing: list[str] = []

    def repl(m: re.Match[str]) -> str:
        key = m.group(1)
        if key in table:
            return table[key]
        missing.append(key)
        return m.group(0)

    out = _PLACEHOLDER.sub(repl, value)
    if missing:
        raise LaunchError(
            f"Unresolved placeholder(s) {', '.join(sorted(set(missing)))} in "
            f"{where or 'argument'}: {value!r}. This version needs a value the launcher "
            f"does not supply yet.")
    return out


def _flatten(entry, table: dict[str, str], os_info: OsInfo, features: Features,
             where: str) -> list[str]:
    """One entry from ``arguments.game``/``arguments.jvm`` -> zero or more real arguments."""
    if isinstance(entry, str):
        return [substitute(entry, table, where=where)]
    if isinstance(entry, dict):
        if not rules.allowed(entry.get("rules"), os_info=os_info, features=features):
            return []
        value = entry.get("value")
        if value is None:
            return []
        if isinstance(value, str):
            value = [value]
        return [substitute(v, table, where=where) for v in value]
    return []


def build_arguments(version: dict, table: dict[str, str], *, os_info: OsInfo,
                    features: Features) -> tuple[list[str], list[str]]:
    """Return ``(jvm_args, game_args)`` for either argument era.

    Pre-1.13 versions carry a single ``minecraftArguments`` string and no JVM block at all,
    so the JVM side is synthesised. 1.13+ versions carry ``arguments: {game, jvm}`` where
    each element is a plain string or a rules-gated object.
    """
    args = version.get("arguments") or {}
    jvm: list[str] = []
    game: list[str] = []

    if args.get("jvm"):
        for entry in args["jvm"]:
            jvm.extend(_flatten(entry, table, os_info, features, "arguments.jvm"))
    else:
        # The shape every pre-1.13 version implies but never writes down.
        jvm.extend([
            f"-Djava.library.path={table['natives_directory']}",
            "-cp", table["classpath"],
        ])

    if args.get("game"):
        for entry in args["game"]:
            game.extend(_flatten(entry, table, os_info, features, "arguments.game"))
    elif version.get("minecraftArguments"):
        for token in str(version["minecraftArguments"]).split():
            game.append(substitute(token, table, where="minecraftArguments"))

    return jvm, game


def placeholder_table(*, version: dict, instance_dir: Path, layout: Layout,
                      account: Account, classpath: str, natives_dir: Path,
                      asset_index_id: str, game_assets: Path | None,
                      client_id: str, width: int, height: int,
                      os_info: OsInfo, quick_play: str = "",
                      quick_play_path: str = "") -> dict[str, str]:
    """Every placeholder from section 7.2, in one place.

    All ``auth_*`` values come from the :class:`Account` interface. Nothing in this module
    reaches for a token any other way.
    """
    return {
        "auth_player_name": account.name,
        "auth_uuid": account.uuid_dashless,
        "auth_access_token": account.access_token,
        "auth_session": f"token:{account.access_token}:{account.uuid_dashless}",
        "auth_xuid": account.xuid,
        "user_type": account.user_type,
        "user_properties": "{}",
        "clientid": client_id,
        "version_name": version.get("id", ""),
        "version_type": version.get("type", "release"),
        "profile_name": APP_NAME,
        "game_directory": str(instance_dir),
        "assets_root": str(layout.assets),
        "assets_index_name": asset_index_id,
        "game_assets": str(game_assets or layout.assets / "virtual" / "legacy"),
        "natives_directory": str(natives_dir),
        "classpath": classpath,
        "classpath_separator": os_info.classpath_separator,
        "library_directory": str(layout.libraries),
        "launcher_name": APP_NAME.replace(" ", ""),
        "launcher_version": APP_VERSION,
        "resolution_width": str(width or 854),
        "resolution_height": str(height or 480),
        # Quick Play. 1.20+ emits these arguments itself, gated on the matching feature
        # flags, so the launcher supplies values rather than arguments.
        "quickPlayPath": quick_play_path,
        "quickPlayMultiplayer": quick_play,
        "quickPlaySingleplayer": "",
        "quickPlayRealms": "",
    }


def _mc_version_tuple(version: dict) -> tuple[int, ...]:
    """Best-effort numeric version, used only for the Log4Shell cutoff."""
    raw = version.get("inheritsFrom") or version.get("id") or ""
    m = re.match(r"(\d+)\.(\d+)(?:\.(\d+))?", raw)
    if not m:
        return (99, 0, 0)  # unknown: treat as modern, do not add legacy flags
    return tuple(int(g or 0) for g in m.groups())


def log4j_flags(version: dict) -> list[str]:
    """Close Log4Shell on the versions that never got a patched build (<= 1.18.1)."""
    v = _mc_version_tuple(version)
    if v >= (1, 18, 2):
        return []
    return ["-Dlog4j2.formatMsgNoLookups=true"]


# ---------------------------------------------------------------------------------
# The plan
# ---------------------------------------------------------------------------------

@dataclass
class LaunchPlan:
    """Everything resolved and verified, ready to spawn. Built by :func:`prepare`."""
    instance: Instance
    version: dict
    java: Path
    argv: list[str]
    cwd: Path
    natives_dir: Path
    log_file: Path
    account_summary: object
    env: dict[str, str] = field(default_factory=dict)

    #: Flags whose *next* argv element is a secret. Redaction by pattern cannot see this,
    #: because an access token in its own list element looks like any other opaque blob.
    SECRET_FLAGS = ("--accessToken", "--session", "--auth_access_token")

    def redacted_argv(self) -> list[str]:
        """The command line with the token replaced -- safe to log or show."""
        out: list[str] = []
        redact_next = False
        for a in self.argv:
            if redact_next and a != "0":  # "0" is the dev stub's placeholder, not a secret
                out.append(logs.MARK)
            else:
                out.append(logs.redact(a))
            redact_next = a in self.SECRET_FLAGS
        return out


def prepare(layout: Layout, instance: Instance, account: Account, *,
            settings: Settings | None = None,
            progress: net.Progress | None = None,
            cancel: net.CancelToken = net.NEVER,
            quick_play_server: str | None = None,
            offline_assets_ok: bool = False) -> LaunchPlan:
    """Resolve, download, verify and assemble. Does everything except spawn.

    Split from :func:`spawn` so a test can assert the exact command line without starting
    a game, which is what makes the argument rules testable at all.
    """
    settings = settings or Settings()
    progress = progress or net.Progress()
    os_info = rules.current_os()

    instance.ensure_dirs()
    game_dir = guard_game_dir(instance.dir)

    # --- resolve -----------------------------------------------------------------
    progress.set_phase("Resolving", instance.version_id)
    version = versions.resolve(layout, instance.version_id, cancel=cancel)

    quick = quick_play_server if quick_play_server is not None else instance.quick_play_server
    features = Features(
        has_custom_resolution=bool(instance.width and instance.height),
        has_quick_plays_support=bool(quick) and _supports_quick_play(version),
        is_quick_play_multiplayer=bool(quick) and _supports_quick_play(version),
    )

    # --- java --------------------------------------------------------------------
    java_path = java_mod.resolve_java(
        layout, version, override=instance.java_override or settings.java_override or None,
        progress=progress, cancel=cancel)

    # --- libraries + client jar ---------------------------------------------------
    jobs, lib_files = lib_mod.plan_downloads(version, layout, os_info=os_info,
                                             features=features)
    if jobs:
        net.run_jobs(jobs, concurrency=settings.download_concurrency, progress=progress,
                     phase="Downloading libraries", cancel=cancel)

    absent = lib_mod.missing_local(lib_files)
    if absent:
        names = ", ".join(f.name for f in absent[:5])
        raise LaunchError(
            f"{len(absent)} librar{'y' if len(absent) == 1 else 'ies'} named by "
            f"{instance.version_id} have no download URL and are not on disk ({names}). "
            f"That normally means a loader installation did not finish -- reinstall the "
            f"loader for this instance.")

    # --- assets -------------------------------------------------------------------
    progress.set_phase("Checking assets")
    asset_index = None
    game_assets = None
    try:
        asset_index = assets_mod.ensure_index(version, layout, cancel=cancel)
    except net.NetError:
        if not offline_assets_ok:
            raise
        log.warning("asset index unavailable; launching with whatever is cached")
    if asset_index is not None:
        asset_jobs = assets_mod.plan_downloads(asset_index, layout)
        if asset_jobs:
            net.run_jobs(asset_jobs, concurrency=settings.download_concurrency,
                         progress=progress, phase="Downloading assets", cancel=cancel)
        game_assets = assets_mod.materialise_legacy(asset_index, layout, game_dir)

    asset_index_id = (asset_index.id if asset_index
                      else (version.get("assets") or "legacy"))

    # --- natives ------------------------------------------------------------------
    progress.set_phase("Extracting natives")
    natives_dir = layout.natives_root / f"{instance.slug}-{os.getpid()}-{int(time.time())}"
    lib_mod.extract_natives(lib_files, natives_dir)

    # --- command line --------------------------------------------------------------
    progress.set_phase("Building command line")
    # Must happen after the downloads: modern Forge's -DignoreList names the client jar by
    # the launched version's filename, so it has to exist under that id. See the function.
    lib_mod.materialise_client_jar(version, layout)
    cp_entries = lib_mod.build_classpath(version, layout, lib_files, os_info=os_info)
    classpath = lib_mod.join_classpath(cp_entries, os_info)

    quick_normalised = _normalise_server(quick) if quick else ""
    table = placeholder_table(
        version=version, instance_dir=game_dir, layout=layout, account=account,
        classpath=classpath, natives_dir=natives_dir, asset_index_id=asset_index_id,
        game_assets=game_assets, client_id=settings.effective_client_id or APP_NAME.lower(),
        width=instance.width, height=instance.height, os_info=os_info,
        quick_play=quick_normalised,
        quick_play_path=str(game_dir / "quickPlay" / "log.json"))

    jvm_args, game_args = build_arguments(version, table, os_info=os_info,
                                          features=features)

    heap = max(512, int(instance.memory_mb or settings.default_memory_mb))
    jvm_args = [
        f"-Xmx{heap}M",
        f"-Xms{min(heap, 512)}M",
        *log4j_flags(version),
        *(["-XstartOnFirstThread"] if os_info.is_osx else []),
        *jvm_args,
    ]
    if not any(a.startswith("-Djava.library.path=") for a in jvm_args):
        jvm_args.insert(0, f"-Djava.library.path={natives_dir}")
    jvm_args.extend(settings.extra_jvm_args)
    jvm_args.extend(instance.extra_jvm_args)

    if instance.width and instance.height and not _has_flag(game_args, "--width"):
        game_args += ["--width", str(instance.width),
                      "--height", str(instance.height)]

    game_args.extend(_quick_play_args(version, quick, game_args))
    game_args.extend(instance.extra_game_args)

    main_class = version.get("mainClass")
    if not main_class:
        raise LaunchError(f"{instance.version_id} declares no mainClass; the version JSON "
                          f"is incomplete.")

    argv = [str(java_path), *jvm_args, main_class, *game_args]

    mkdirs(game_dir / "logs")
    log_file = game_dir / "logs" / f"myfire-{time.strftime('%Y%m%d-%H%M%S')}.log"

    progress.set_phase("Starting", instance.name)
    return LaunchPlan(instance=instance, version=version, java=java_path, argv=argv,
                      cwd=game_dir, natives_dir=natives_dir, log_file=log_file,
                      account_summary=account.summary())


def _has_flag(args: Sequence[str], flag: str) -> bool:
    return any(a == flag for a in args)


def _supports_quick_play(version: dict) -> bool:
    """1.20+ understands --quickPlayMultiplayer; older versions take --server/--port."""
    args = ((version.get("arguments") or {}).get("game")) or []
    for entry in args:
        if isinstance(entry, dict):
            value = entry.get("value")
            values = [value] if isinstance(value, str) else (value or [])
            if any("quickPlayMultiplayer" in str(v) for v in values):
                return True
    return False


def _normalise_server(server: str) -> str:
    host, _, port = server.partition(":")
    return f"{host}:{port or '25565'}"


def _quick_play_args(version: dict, server: str | None,
                     existing: Sequence[str]) -> list[str]:
    """Join a server straight from Play.

    1.20+ declares the Quick Play arguments in its own ``arguments.game`` block, gated on
    the ``is_quick_play_multiplayer`` feature -- so for those versions the arguments are
    already present and this adds nothing. Older versions declare nothing, and take the
    original ``--server``/``--port`` pair instead.
    """
    if not server:
        return []
    if _has_flag(existing, "--quickPlayMultiplayer") or _has_flag(existing, "--server"):
        return []  # the version's own rules already emitted it
    if _supports_quick_play(version):
        return ["--quickPlayMultiplayer", _normalise_server(server)]
    host, _, port = server.partition(":")
    return ["--server", host, "--port", port or "25565"]


# ---------------------------------------------------------------------------------
# Spawning and supervision
# ---------------------------------------------------------------------------------

class GameProcess:
    """A running game, plus a tail of its log file.

    Output goes to a file rather than a pipe so the game survives the launcher closing:
    a pipe's read end dies with us and the child's next write fails.
    """

    def __init__(self, popen: subprocess.Popen, plan: LaunchPlan) -> None:
        self.popen = popen
        self.plan = plan
        self.started = time.time()
        self.lines: deque[str] = deque(maxlen=500)
        self._listeners: list[Callable[[str], None]] = []
        self._stop = threading.Event()
        self._reader = threading.Thread(target=self._tail, name="game-log", daemon=True)
        self._reader.start()
        self._reaper = threading.Thread(target=self._reap, name="game-reap", daemon=True)
        self._reaper.start()
        self.on_exit: list[Callable[[int], None]] = []

    # -- state ----------------------------------------------------------------------
    @property
    def pid(self) -> int:
        return self.popen.pid

    @property
    def running(self) -> bool:
        return self.popen.poll() is None

    @property
    def exit_code(self) -> int | None:
        return self.popen.poll()

    def listen(self, fn: Callable[[str], None]) -> None:
        self._listeners.append(fn)

    def tail_lines(self, n: int = 40) -> list[str]:
        return list(self.lines)[-n:]

    def kill(self) -> None:
        try:
            self.popen.kill()
        except OSError:
            pass

    def wait(self, timeout: float | None = None) -> int | None:
        try:
            return self.popen.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            return None

    # -- internals ------------------------------------------------------------------
    def _tail(self) -> None:
        path = self.plan.log_file
        pending = ""
        pos = 0
        while not self._stop.is_set():
            try:
                with open(ext(path), "r", encoding="utf-8", errors="replace") as fh:
                    fh.seek(pos)
                    chunk = fh.read()
                    pos = fh.tell()
            except OSError:
                chunk = ""
            if chunk:
                pending += chunk
                *complete, pending = pending.split("\n")
                for line in complete:
                    line = line.rstrip("\r")
                    self.lines.append(line)
                    for fn in tuple(self._listeners):
                        try:
                            fn(line)
                        except Exception:
                            pass
            elif not self.running:
                break
            time.sleep(0.25)
        self._stop.set()

    def _reap(self) -> None:
        code = self.popen.wait()
        time.sleep(0.6)  # let the tail thread drain the last writes
        self._stop.set()
        from .libraries import cleanup_natives
        cleanup_natives(self.plan.natives_dir)
        if code != 0:
            log.warning("game exited with code %s", code)
        for fn in tuple(self.on_exit):
            try:
                fn(code)
            except Exception:
                log.debug("exit listener raised", exc_info=True)

    def diagnose(self) -> str:
        """A human-readable verdict for a non-zero exit, or an instant clean one.

        Exit code 0 with an instant close is almost always a natives or classpath failure,
        so the last lines are surfaced rather than left in a file nobody opens.
        """
        code = self.exit_code
        elapsed = time.time() - self.started
        tail = "\n".join(self.tail_lines(40))
        if code is None:
            return "still running"
        if code == 0 and elapsed < 20:
            return ("The game exited immediately with code 0. That is almost always a "
                    "natives or classpath problem rather than a crash.\n\n" + tail)
        if code != 0:
            return f"The game exited with code {code}.\n\n{tail}"
        return "The game exited normally."


def spawn(plan: LaunchPlan, *, extra_env: dict[str, str] | None = None) -> GameProcess:
    """Start the game. Detached, own process group, output to a file."""
    mkdirs(plan.log_file.parent)
    env = os.environ.copy()
    env.update(plan.env)
    if extra_env:
        env.update(extra_env)

    creationflags = 0
    if sys.platform == "win32":
        # DETACHED_PROCESS: no console window and no dependence on ours.
        # CREATE_NEW_PROCESS_GROUP: a Ctrl-C in the launcher's terminal is not the game's.
        creationflags = (getattr(subprocess, "DETACHED_PROCESS", 0)
                         | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))

    log.info("launching %s as %s", plan.instance.name, plan.account_summary)
    log.debug("argv: %s", plan.redacted_argv())

    handle = open(ext(plan.log_file), "wb", buffering=0)
    try:
        popen = subprocess.Popen(
            plan.argv,               # an array. never a string. see the module docstring.
            cwd=str(plan.cwd),
            stdout=handle,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            env=env,
            close_fds=True,
            creationflags=creationflags,
            start_new_session=(sys.platform != "win32"),
        )
    except OSError as exc:
        handle.close()
        raise LaunchError(
            f"Could not start Java: {exc}\n  executable: {plan.argv[0]}") from exc
    finally:
        try:
            handle.close()   # the child holds its own duplicate
        except OSError:
            pass

    # Stamp the natives directory with the game's pid so a later sweep can tell whether it
    # is still in use -- the reaper below dies with the launcher, and closing the launcher
    # mid-session is explicitly allowed.
    from .libraries import claim_natives
    claim_natives(plan.natives_dir, popen.pid)

    plan.instance.touch_played()
    return GameProcess(popen, plan)


def launch(layout: Layout, instance: Instance, account: Account, **kw) -> GameProcess:
    """Convenience: prepare then spawn."""
    spawn_kw = {"extra_env": kw.pop("extra_env", None)}
    return spawn(prepare(layout, instance, account, **kw), **spawn_kw)
