"""HTTP, downloads and integrity.

Everything the launcher fetches goes through here so the rules in section 10 of the brief
hold in exactly one place: bounded concurrency, keep-alive, retry with exponential backoff
and jitter on 5xx/timeouts, *never* retry a 404, resume with ``Range``, verify SHA-1, and
write via a temp file + atomic rename so an antivirus scan or a power cut can't leave a
half-written jar that looks complete.

Because every artefact is content-addressed, a second run over an installed instance is a
cheap no-op: :func:`download` returns immediately when the file is present and its hash
matches, without opening a socket.

Standard library only -- no ``requests``. That keeps the core importable on a bare Python
install, which is what makes the test suite runnable with zero setup.
"""
from __future__ import annotations

import concurrent.futures
import hashlib
import http.client
import json
import os
import random
import socket
import ssl
import threading
import time
import urllib.parse
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable, Sequence

from . import logs
from .paths import APP_NAME, APP_VERSION, ext, mkdirs

log = logs.get("net")

USER_AGENT = f"{APP_NAME.replace(' ', '')}/{APP_VERSION} (+https://localhost; personal)"
DEFAULT_TIMEOUT = 30.0
MAX_ATTEMPTS = 5
MAX_REDIRECTS = 6


class NetError(RuntimeError):
    """Base class for anything this module gives up on."""


class HttpError(NetError):
    def __init__(self, status: int, url: str, body: bytes = b"", reason: str = ""):
        self.status = status
        self.url = url
        self.reason = reason
        self.body = body
        snippet = logs.redact(body[:400].decode("utf-8", "replace")) if body else ""
        super().__init__(f"HTTP {status} {reason} for {url}"
                         + (f"\n{snippet}" if snippet else ""))

    def json(self) -> dict:
        try:
            return json.loads(self.body.decode("utf-8"))
        except Exception:
            return {}


class HashMismatch(NetError):
    def __init__(self, path: Path, expected: str, actual: str):
        self.path, self.expected, self.actual = path, expected, actual
        super().__init__(
            f"SHA-1 mismatch for {path}\n  expected {expected}\n  actual   {actual}\n"
            "The file was deleted. If this repeats, the source or the disk is at fault.")


class Cancelled(NetError):
    pass


class CancelToken:
    """Cooperative cancellation shared by every worker in a download pool."""

    def __init__(self, *parents: "CancelToken") -> None:
        self._event = threading.Event()
        self._parents = parents

    def cancel(self) -> None:
        self._event.set()

    @property
    def cancelled(self) -> bool:
        return self._event.is_set() or any(p.cancelled for p in self._parents)

    def check(self) -> None:
        if self.cancelled:
            raise Cancelled("cancelled")

    def child(self) -> "CancelToken":
        """A token cancelled by this one, but whose own cancellation stays local.

        Batch operations need this: aborting one batch must not poison the caller's
        token (and must never poison the shared :data:`NEVER`).
        """
        return CancelToken(self)


NEVER = CancelToken()


# ---------------------------------------------------------------------------------
# Connection pooling
# ---------------------------------------------------------------------------------

_ssl_ctx = ssl.create_default_context()


class _Pool:
    """One keep-alive connection per (host, thread). Dropped and remade on any error."""

    def __init__(self) -> None:
        self._local = threading.local()

    def _conns(self) -> dict:
        d = getattr(self._local, "conns", None)
        if d is None:
            d = {}
            self._local.conns = d
        return d

    def get(self, scheme: str, host: str, port: int | None, timeout: float):
        key = (scheme, host, port)
        conns = self._conns()
        conn = conns.get(key)
        if conn is None:
            if scheme == "https":
                conn = http.client.HTTPSConnection(
                    host, port, timeout=timeout, context=_ssl_ctx)
            else:
                conn = http.client.HTTPConnection(host, port, timeout=timeout)
            conns[key] = conn
        return conn

    def drop(self, scheme: str, host: str, port: int | None) -> None:
        conn = self._conns().pop((scheme, host, port), None)
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass

    def close_all(self) -> None:
        for conn in self._conns().values():
            try:
                conn.close()
            except Exception:
                pass
        self._conns().clear()


_pool = _Pool()


@dataclass
class Response:
    status: int
    headers: dict
    body: bytes
    url: str

    def json(self) -> dict:
        return json.loads(self.body.decode("utf-8"))


def _sleep_backoff(attempt: int, retry_after: str | None = None) -> None:
    if retry_after:
        try:
            time.sleep(min(30.0, float(retry_after)))
            return
        except ValueError:
            pass
    base = min(16.0, 0.5 * (2 ** attempt))
    time.sleep(base * (0.5 + random.random()))  # full-ish jitter


def request(method: str, url: str, *, body: bytes | None = None,
            headers: dict | None = None, timeout: float = DEFAULT_TIMEOUT,
            expect: Sequence[int] = (200,), retries: int = MAX_ATTEMPTS,
            stream_to: Callable[[bytes], None] | None = None,
            cancel: CancelToken = NEVER) -> Response:
    """One HTTP request with the retry policy applied.

    ``expect`` lists the statuses treated as success; anything else raises
    :class:`HttpError`. 4xx is never retried (a 404 will still be a 404), except 429 and
    408, which are.
    """
    hdrs = {"User-Agent": USER_AGENT, "Accept-Encoding": "identity",
            "Connection": "keep-alive"}
    if headers:
        hdrs.update(headers)

    last_exc: Exception | None = None
    current = url
    for attempt in range(retries):
        cancel.check()
        redirects = 0
        try:
            while True:
                parts = urllib.parse.urlsplit(current)
                scheme, host, port = parts.scheme, parts.hostname, parts.port
                target = parts.path or "/"
                if parts.query:
                    target += "?" + parts.query
                conn = _pool.get(scheme, host, port, timeout)
                try:
                    conn.request(method, target, body=body, headers=hdrs)
                    resp = conn.getresponse()
                except Exception:
                    _pool.drop(scheme, host, port)
                    raise

                status = resp.status
                rheaders = {k.lower(): v for k, v in resp.getheaders()}

                if status in (301, 302, 303, 307, 308) and rheaders.get("location"):
                    resp.read()
                    redirects += 1
                    if redirects > MAX_REDIRECTS:
                        raise NetError(f"too many redirects from {url}")
                    current = urllib.parse.urljoin(current, rheaders["location"])
                    if status == 303 and method not in ("GET", "HEAD"):
                        method, body = "GET", None
                    continue

                if status in expect and stream_to is not None:
                    total = 0
                    while True:
                        cancel.check()
                        chunk = resp.read(1 << 16)
                        if not chunk:
                            break
                        stream_to(chunk)
                        total += len(chunk)
                    return Response(status, rheaders, b"", current)

                payload = resp.read()
                if status in expect:
                    return Response(status, rheaders, payload, current)

                err = HttpError(status, current, payload, resp.reason)
                retryable = status >= 500 or status in (408, 429)
                if not retryable:
                    raise err
                last_exc = err
                if attempt == retries - 1:
                    raise err
                log.debug("retrying %s (%s) attempt %d", current, status, attempt + 1)
                _sleep_backoff(attempt, rheaders.get("retry-after"))
                break
        except Cancelled:
            raise
        except HttpError:
            raise
        except (socket.timeout, TimeoutError, ConnectionError, http.client.HTTPException,
                ssl.SSLError, OSError) as exc:
            last_exc = exc
            if attempt == retries - 1:
                break
            log.debug("retrying %s (%s) attempt %d", current, exc, attempt + 1)
            _sleep_backoff(attempt)

    raise NetError(f"request failed after {retries} attempts: {url}") from last_exc


def get_bytes(url: str, **kw) -> bytes:
    return request("GET", url, **kw).body


def get_json(url: str, **kw) -> dict:
    kw.setdefault("headers", {}).setdefault("Accept", "application/json")
    return json.loads(get_bytes(url, **kw).decode("utf-8"))


def post_json(url: str, payload: dict | None = None, *, form: dict | None = None,
              headers: dict | None = None, expect: Sequence[int] = (200,),
              **kw) -> Response:
    hdrs = dict(headers or {})
    hdrs.setdefault("Accept", "application/json")
    if form is not None:
        body = urllib.parse.urlencode(form).encode()
        hdrs["Content-Type"] = "application/x-www-form-urlencoded"
    else:
        body = json.dumps(payload or {}).encode()
        hdrs["Content-Type"] = "application/json"
    return request("POST", url, body=body, headers=hdrs, expect=expect, **kw)


# ---------------------------------------------------------------------------------
# Files
# ---------------------------------------------------------------------------------

def sha1_file(path: Path | str, _bufsize: int = 1 << 20) -> str:
    h = hashlib.sha1()
    with open(ext(path), "rb") as fh:
        while True:
            chunk = fh.read(_bufsize)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def verify(path: Path | str, sha1: str | None, size: int | None = None) -> bool:
    """True if the file on disk is already the artefact we wanted."""
    try:
        st = os.stat(ext(path))
    except OSError:
        return False
    if size is not None and st.st_size != size:
        return False
    if sha1:
        return sha1_file(path).lower() == sha1.lower()
    return st.st_size > 0


def write_atomic(path: Path | str, data: bytes) -> None:
    """Write via a sibling temp file and rename. Never leaves a partial file in place."""
    path = Path(path)
    mkdirs(path.parent)
    tmp = path.with_name(path.name + f".{os.getpid()}.{threading.get_ident()}.part")
    with open(ext(tmp), "wb") as fh:
        fh.write(data)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(ext(tmp), ext(path))


def download(url: str, dest: Path | str, *, sha1: str | None = None,
             size: int | None = None, cancel: CancelToken = NEVER,
             on_bytes: Callable[[int], None] | None = None,
             resume: bool = True, timeout: float = DEFAULT_TIMEOUT) -> bool:
    """Fetch ``url`` to ``dest``, verified. Returns True if bytes were transferred.

    A present-and-correct file short-circuits with no network at all -- this is what makes
    the "second launch downloads nothing" guarantee true.
    """
    dest = Path(dest)
    if verify(dest, sha1, size):
        if on_bytes and size:
            on_bytes(size)
        return False

    mkdirs(dest.parent)
    tmp = dest.with_name(dest.name + f".{os.getpid()}.{threading.get_ident()}.part")

    allow_resume = resume
    for attempt in range(3):  # one retry for a hash mismatch, one for a refused Range
        cancel.check()
        start = 0
        mode = "wb"
        if allow_resume and attempt == 0:
            try:
                existing = os.path.getsize(ext(tmp))
                if size is None or 0 < existing < size:
                    start, mode = existing, "ab"
            except OSError:
                pass
        else:
            try:
                os.unlink(ext(tmp))
            except OSError:
                pass

        headers = {"Range": f"bytes={start}-"} if start else {}
        expect = (200, 206) if start else (200,)

        range_refused = False
        with open(ext(tmp), mode) as fh:
            def sink(chunk: bytes, _fh=fh) -> None:
                _fh.write(chunk)
                if on_bytes:
                    on_bytes(len(chunk))

            resp = request("GET", url, headers=headers, expect=expect,
                           stream_to=sink, cancel=cancel, timeout=timeout)
            if start and resp.status == 200:
                # The server ignored Range and sent the whole body, which we appended to
                # bytes we already had. Everything written this pass is garbage.
                range_refused = True
        if range_refused:
            log.debug("server ignored Range for %s; restarting whole file", url)
            allow_resume = False
            if on_bytes and start:
                on_bytes(-start)
            try:
                os.unlink(ext(tmp))
            except OSError:
                pass
            continue

        if sha1:
            actual = sha1_file(tmp)
            if actual.lower() != sha1.lower():
                try:
                    os.unlink(ext(tmp))
                except OSError:
                    pass
                if attempt == 0:
                    log.warning("hash mismatch on %s, retrying once", dest.name)
                    if on_bytes and size:
                        on_bytes(-size)  # undo the progress we just claimed
                    continue
                raise HashMismatch(dest, sha1, actual)

        os.replace(ext(tmp), ext(dest))
        return True

    raise NetError(f"download failed: {url}")  # pragma: no cover - unreachable


# ---------------------------------------------------------------------------------
# Progress + bounded pool
# ---------------------------------------------------------------------------------

@dataclass
class Progress:
    """Byte-weighted progress that is honest about what it does not know yet.

    ``total_bytes`` is the sum of the sizes the manifests declared, so the percentage is
    real rather than a count of files pretending to be one. Files already on disk are
    credited up front, which is why a no-op run jumps straight to 100%.
    """
    phase: str = "Idle"
    done_items: int = 0
    total_items: int = 0
    done_bytes: int = 0
    total_bytes: int = 0
    detail: str = ""
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    _listeners: list = field(default_factory=list, repr=False)

    def listen(self, fn: Callable[["Progress"], None]) -> None:
        self._listeners.append(fn)

    def _emit(self) -> None:
        for fn in tuple(self._listeners):
            try:
                fn(self)
            except Exception:
                log.debug("progress listener raised", exc_info=True)

    def begin(self, phase: str, total_items: int = 0, total_bytes: int = 0,
              detail: str = "") -> None:
        with self._lock:
            self.phase, self.detail = phase, detail
            self.done_items, self.total_items = 0, total_items
            self.done_bytes, self.total_bytes = 0, total_bytes
        self._emit()

    def add_bytes(self, n: int) -> None:
        with self._lock:
            self.done_bytes += n
        self._emit()

    def item_done(self, detail: str = "") -> None:
        with self._lock:
            self.done_items += 1
            if detail:
                self.detail = detail
        self._emit()

    def set_phase(self, phase: str, detail: str = "") -> None:
        with self._lock:
            self.phase, self.detail = phase, detail
        self._emit()

    @property
    def fraction(self) -> float:
        if self.total_bytes:
            return min(1.0, self.done_bytes / self.total_bytes)
        if self.total_items:
            return min(1.0, self.done_items / self.total_items)
        return 0.0

    def describe(self) -> str:
        if self.total_items:
            return f"{self.phase} {self.done_items}/{self.total_items}"
        return self.phase


@dataclass
class Job:
    url: str
    dest: Path
    sha1: str | None = None
    size: int | None = None
    label: str = ""


def run_jobs(jobs: Iterable[Job], *, concurrency: int = 12,
             progress: Progress | None = None, phase: str = "Downloading",
             cancel: CancelToken = NEVER) -> int:
    """Run downloads through a bounded pool. Returns the number of files transferred.

    Concurrency is bounded because thousands of tiny asset writes against Windows
    real-time scanning is the slow path, and unbounded threads make it slower, not faster.
    """
    jobs = list(jobs)
    if not jobs:
        return 0
    total_bytes = sum(j.size or 0 for j in jobs)
    if progress:
        progress.begin(phase, total_items=len(jobs), total_bytes=total_bytes)

    transferred = 0
    lock = threading.Lock()
    errors: list[BaseException] = []
    batch = cancel.child()

    def work(job: Job) -> None:
        nonlocal transferred
        try:
            did = download(job.url, job.dest, sha1=job.sha1, size=job.size,
                           cancel=batch,
                           on_bytes=progress.add_bytes if progress else None)
        except BaseException as exc:  # noqa: BLE001 - collected and re-raised below
            with lock:
                errors.append(exc)
            # One failure stops the batch: continuing to hammer a dead endpoint with 3000
            # asset requests turns a clear error into a five-minute one. The token is a
            # child, so the caller's own token is untouched.
            batch.cancel()
            return
        if did:
            with lock:
                transferred += 1
        if progress:
            progress.item_done(job.label or job.dest.name)

    workers = max(1, min(int(concurrency), 32))
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers,
                                               thread_name_prefix="dl") as pool:
        list(pool.map(work, jobs))

    if errors:
        for exc in errors:
            if isinstance(exc, Cancelled):
                raise exc
        raise errors[0]
    return transferred
