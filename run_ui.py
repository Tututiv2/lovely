"""Entry point for the .bat wrapper.

Kept as a file rather than ``-m launcher ui`` so a double-click never depends on the
working directory, and so an import error surfaces in a window instead of a console that
closes instantly.
"""
from __future__ import annotations

import os
import sys
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))


def main() -> int:
    if "--dev" in sys.argv:
        os.environ["MYFIRE_DEV"] = "1"
    try:
        from launcher.paths import Layout
        from launcher.ui.app import main as ui_main
        from launcher.ui.watchdog import redirect_stdio
        layout = Layout()
        # pythonw.exe leaves sys.stdout/sys.stderr as None. Anything that writes to them
        # raises on None.write instead of being recorded, which is how a crash becomes
        # invisible. Give them a file before importing anything that might log.
        redirect_stdio(layout.logs / "stdio.log")
        return ui_main(layout)
    except Exception:
        detail = traceback.format_exc()
        try:
            import tkinter as tk
            from tkinter import messagebox
            root = tk.Tk()
            root.withdraw()
            messagebox.showerror("Lovely could not start", detail[-2000:])
        except Exception:
            print(detail, file=sys.stderr)
            input("\nPress Enter to close...")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
