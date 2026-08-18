"""Headless entry point -- the one that exists so milestone 1 could be proven before any UI.

The UI is a client of this same core; nothing here imports tkinter. ``dryrun`` in
particular is the command that makes argument building auditable: it prints the exact argv
that would be handed to :class:`subprocess.Popen`, with the token redacted, without
starting a game.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from pathlib import Path

from . import logs, net, versions
from .accounts import AuthError, LocalAccount, dev_mode_enabled
from .accounts.local import DEV_ENV_FLAG
from .instances import InstanceStore, vanilla_minecraft_dir
from .paths import APP_NAME, APP_VERSION, Layout
from .settings import Settings


def _progress_printer() -> net.Progress:
    p = net.Progress()
    state = {"last": 0.0, "phase": ""}

    def show(pr: net.Progress) -> None:
        now = time.time()
        changed = pr.phase != state["phase"]
        if not changed and now - state["last"] < 0.25:
            return
        state["last"], state["phase"] = now, pr.phase
        pct = int(pr.fraction * 100)
        line = f"  {pr.describe():<34} {pct:3d}%  {pr.detail[:44]}"
        sys.stdout.write("\r" + line.ljust(100))
        sys.stdout.flush()
        if changed:
            sys.stdout.write("\n")

    p.listen(show)
    return p


def _account(args) -> LocalAccount:
    if args.dev_user:
        if not dev_mode_enabled():
            os.environ[DEV_ENV_FLAG] = "1"  # --dev-user is itself the opt-in
        return LocalAccount(args.dev_user)
    raise AuthError(
        "No account. Real Microsoft login needs the Azure app to be approved for the "
        "Minecraft API (see README section 1). Until then pass --dev-user <name> to test "
        "against a local server running online-mode=false.")


def cmd_versions(args, layout: Layout) -> int:
    manifest = versions.load_manifest(layout, refresh=args.refresh)
    types = args.type or (["release"] if not args.all else None)
    entries = manifest.filtered(types)
    print(f"latest release {manifest.latest_release}   "
          f"latest snapshot {manifest.latest_snapshot}")
    for v in entries[:args.limit]:
        print(f"  {v.id:<24} {v.type:<10} {v.release_time[:10]}")
    print(f"  ... {len(entries)} matching versions")
    return 0


def cmd_list(args, layout: Layout) -> int:
    store = InstanceStore(layout)
    rows = store.all()
    if not rows:
        print("No instances yet. Create one with:  create <name> --version 1.20.1")
        return 0
    for i in rows:
        played = (time.strftime("%Y-%m-%d %H:%M", time.localtime(i.last_played))
                  if i.last_played else "never")
        print(f"  {i.slug:<28} {i.badge:<24} {i.memory_mb:>6} MB   last played {played}")
    print(f"\n  instances root: {layout.instances}")
    return 0


def cmd_create(args, layout: Layout) -> int:
    from .loaders import install_loader
    store = InstanceStore(layout)
    version_id = args.version
    mc_version = args.version
    if args.loader != "vanilla":
        version_id = install_loader(layout, args.loader, args.version,
                                    loader_version=args.loader_version or None,
                                    progress=_progress_printer())
    inst = store.create(args.name, version_id, mc_version=mc_version,
                        loader=args.loader, loader_version=args.loader_version or "",
                        memory_mb=args.memory)
    print(f"created {inst.slug}  ->  {inst.dir}")
    print(f"  mods folder: {inst.mods_dir}")
    return 0


def cmd_dryrun(args, layout: Layout) -> int:
    from .launch import prepare
    store = InstanceStore(layout)
    inst = store.get(args.slug)
    if inst is None:
        print(f"no instance {args.slug!r}", file=sys.stderr)
        return 2
    plan = prepare(layout, inst, _account(args), settings=Settings.load(layout),
                   progress=_progress_printer(), quick_play_server=args.server)
    print("\n\njava:", plan.java)
    print("cwd :", plan.cwd)
    print("argv:")
    for a in plan.redacted_argv():
        print("   ", a if len(a) < 220 else a[:200] + f"... ({len(a)} chars)")
    return 0


def cmd_launch(args, layout: Layout) -> int:
    from .launch import launch
    store = InstanceStore(layout)
    inst = store.get(args.slug)
    if inst is None:
        print(f"no instance {args.slug!r}", file=sys.stderr)
        return 2
    proc = launch(layout, inst, _account(args), settings=Settings.load(layout),
                  progress=_progress_printer(), quick_play_server=args.server)
    print(f"\n\nstarted pid {proc.pid}, logging to {proc.plan.log_file}")
    if args.detach:
        return 0
    proc.listen(lambda line: print("  |", line))
    deadline = time.time() + args.wait if args.wait else None
    while proc.running:
        if deadline and time.time() > deadline:
            print(f"\n[still running after {args.wait}s -- leaving it up]")
            return 0
        time.sleep(0.5)
    print("\n" + proc.diagnose())
    return 0 if proc.exit_code == 0 else 1


def cmd_doctor(args, layout: Layout) -> int:
    """Environment report. Also proves the untouchable folder is being left alone."""
    print(f"{APP_NAME} {APP_VERSION}")
    print(f"  python       {sys.version.split()[0]}  ({sys.executable})")
    print(f"  data root    {layout.data_root}")
    print(f"  instances    {layout.instances}")
    vanilla = vanilla_minecraft_dir()
    print(f"  vanilla dir  {vanilla}  <- never written to by this launcher")
    if vanilla and (vanilla / "mods").is_dir():
        mods = sorted(p.name for p in (vanilla / "mods").glob("*.jar"))
        print(f"               {len(mods)} mod jar(s) there, left untouched")
    from . import rules as r
    o = r.current_os()
    print(f"  platform     {o.name}/{o.arch} version {o.version!r}  cp sep {o.classpath_separator!r}")
    from . import java as j
    print(f"  java key     {j.platform_key()}")
    for comp in ("jre-legacy", "java-runtime-gamma", "java-runtime-delta"):
        state = "installed" if j.is_installed(layout, comp) else "-"
        print(f"    {comp:<26} {state}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser("launcher", description=f"{APP_NAME} {APP_VERSION}")
    p.add_argument("--data-root", default=None, help="override the data directory")
    p.add_argument("--dev", action="store_true",
                   help="enable the local dev account (testing against online-mode=false)")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)

    v = sub.add_parser("versions", help="list Minecraft versions")
    v.add_argument("--type", action="append", choices=["release", "snapshot",
                                                       "old_beta", "old_alpha"])
    v.add_argument("--all", action="store_true")
    v.add_argument("--limit", type=int, default=25)
    v.add_argument("--refresh", action="store_true")
    v.set_defaults(func=cmd_versions)

    sub.add_parser("list", help="list instances").set_defaults(func=cmd_list)

    c = sub.add_parser("create", help="create an instance")
    c.add_argument("name")
    c.add_argument("--version", required=True)
    c.add_argument("--loader", default="vanilla",
                   choices=["vanilla", "fabric", "forge", "neoforge", "quilt"])
    c.add_argument("--loader-version", default="")
    c.add_argument("--memory", type=int, default=4096)
    c.set_defaults(func=cmd_create)

    for name, fn, doc in (("dryrun", cmd_dryrun, "resolve and print the command line"),
                          ("launch", cmd_launch, "resolve and start the game")):
        s = sub.add_parser(name, help=doc)
        s.add_argument("slug")
        s.add_argument("--dev-user", default=None,
                       help="dev-stub username (local, online-mode=false servers only)")
        s.add_argument("--server", default=None, help="quick play host:port")
        if name == "launch":
            s.add_argument("--detach", action="store_true")
            s.add_argument("--wait", type=float, default=0.0,
                           help="seconds to stream output before returning")
        s.set_defaults(func=fn)

    sub.add_parser("doctor", help="environment report").set_defaults(func=cmd_doctor)

    u = sub.add_parser("ui", help="open the desktop app")
    u.set_defaults(func=lambda a, l: __import__(
        "launcher.ui.app", fromlist=["main"]).main(l))
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.dev:
        os.environ[DEV_ENV_FLAG] = "1"
    layout = Layout(args.data_root) if args.data_root else Layout()
    layout.ensure()
    logs.setup(layout.logs, level=logging.DEBUG if args.verbose else logging.INFO)
    from . import libraries as _lib
    _lib.sweep_stale_natives(layout.natives_root)
    try:
        return args.func(args, layout)
    except (AuthError, net.NetError, RuntimeError) as exc:
        print(f"\n{type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\ncancelled", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
