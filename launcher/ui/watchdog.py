"""Make a frozen UI explain itself.

A desktop app that is double-clicked has no console. Under ``pythonw.exe`` ``sys.stderr``
is ``None``, so a traceback goes nowhere at all -- and a *hang* does not even produce a
traceback. Windows simply closes the window with Application Error 1002, "stopped
interacting with Windows and was closed", which from the outside is indistinguishable from
a crash. The user reports "it crashed" and there is nothing on disk to look at.

This module makes both cases self-reporting:

* :class:`Watchdog` -- a daemon thread that notices when the Tk thread has stopped ticking
  and writes **the stack it is stuck in** to the log. That turns "it crashed after ten
  seconds" into a file naming the exact function, which is the difference between a guess
  and a fix.
* :func:`install_tk_error_handler` -- Tk routes exceptions raised inside a callback to
  ``Tk.report_callback_exception``, whose default implementation prints to ``sys.stderr``.
  With no stderr that is a silent swallow, so it is replaced with one that logs and, once
  per session, tells the user where the log is.

The watchdog costs one sleeping thread and one integer store per UI tick.
"""
from __future__ import annotations

import sys
import threading
import time
import traceback

from .. import logs

log = logs.get("ui.watchdog")


class Watchdog:
    """Notices a stalled Tk thread and records where it is stuck.

    ``beat()`` is called from the UI's own timer. If too long passes without one, the main
    thread is not returning to the event loop -- it is blocked in a callback, or spinning
    in one. Either way the stack is the answer.
    """

    def __init__(self, *, stall_seconds: float = 5.0, poll_seconds: float = 1.5) -> None:
        self.stall = stall_seconds
        self.poll = poll_seconds
        self._last = time.monotonic()
        self._main_id = threading.get_ident()
        self._stop = threading.Event()
        self._reported = False
        self._thread: threading.Thread | None = None

    def beat(self) -> None:
        self._last = time.monotonic()
        self._reported = False

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, name="ui-watchdog",
                                        daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        while not self._stop.wait(self.poll):
            idle = time.monotonic() - self._last
            if idle < self.stall or self._reported:
                continue
            self._reported = True  # one report per stall, not one per poll
            log.error("UI thread has not ticked for %.1fs -- it is stuck. Stack follows.",
                      idle)
            for line in self._main_stack():
                log.error("    %s", line.rstrip())

    def _main_stack(self) -> list[str]:
        frame = sys._current_frames().get(self._main_id)
        if frame is None:
            return ["<main thread frame unavailable>"]
        try:
            return traceback.format_stack(frame)
        except Exception:  # pragma: no cover - diagnostics must never raise
            return ["<stack unavailable>"]


def install_tk_error_handler(root, on_first_error=None) -> None:
    """Route Tk callback exceptions to the log instead of a stderr that does not exist."""
    state = {"shown": False}

    def report(exc_type, exc_value, exc_tb):
        text = "".join(traceback.format_exception(exc_type, exc_value, exc_tb))
        log.error("unhandled exception in a Tk callback:\n%s", text)
        if not state["shown"] and on_first_error is not None:
            state["shown"] = True
            try:
                on_first_error(text)
            except Exception:
                log.debug("error reporter itself failed", exc_info=True)

    root.report_callback_exception = report


def redirect_stdio(path) -> None:
    """Give ``sys.stdout``/``sys.stderr`` somewhere real to go under ``pythonw.exe``.

    Both are ``None`` there. Anything that writes to them -- a stray ``print``, a library's
    warning, Tk's own default error printer -- raises ``AttributeError`` on ``None.write``
    instead of being recorded. Pointing them at a file costs nothing and means a traceback
    that escapes every other net still lands somewhere.
    """
    if sys.stderr is not None and sys.stdout is not None:
        return
    try:
        from ..paths import ext, mkdirs
        mkdirs(path.parent)
        stream = open(ext(path), "a", encoding="utf-8", buffering=1)
        if sys.stdout is None:
            sys.stdout = stream
        if sys.stderr is None:
            sys.stderr = stream
    except Exception:  # pragma: no cover - best effort, never fatal
        pass
