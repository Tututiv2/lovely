"""Ask a Minecraft server whether it is up, and who is on it.

This is the same query the multiplayer list makes: the **Server List Ping**, spoken over a
plain TCP socket. Two packets out, one JSON document back. No auth, no session, nothing
account-related -- which is why the launcher can do it for a server it has never joined.

The wire format is Minecraft's own, not HTTP:

* every integer is a **VarInt** -- seven bits of payload per byte, high bit set to mean
  "another byte follows". Lengths, packet ids and the protocol version all use it.
* every packet is ``VarInt(length) || VarInt(packet_id) || payload``.
* strings are ``VarInt(byte length) || UTF-8``.

Sending protocol version ``-1`` means "I am not telling you what version I am", which is
the conventional way to ask for status without a server refusing on version grounds. Real
servers answer it happily, including the modded ones.

Everything here is bounded: a hard read cap, a socket timeout, and a limit on VarInt length
so a hostile or broken server cannot make the launcher hang or allocate without limit.
"""
from __future__ import annotations

import json
import re
import socket
import struct
import time
from dataclasses import dataclass, field

from . import logs

log = logs.get("serverping")

DEFAULT_PORT = 25565
MAX_RESPONSE = 512 * 1024      # a status document is a few KB; this is a hostile-input cap
MAX_VARINT_BYTES = 5


class PingError(RuntimeError):
    pass


# ---------------------------------------------------------------------------------
# Wire primitives
# ---------------------------------------------------------------------------------

def encode_varint(value: int) -> bytes:
    if value < 0:
        value += 1 << 32                       # VarInts are two's complement over 32 bits
    out = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        if value:
            out.append(byte | 0x80)
        else:
            out.append(byte)
            return bytes(out)


def read_varint(sock: socket.socket) -> int:
    value = 0
    for i in range(MAX_VARINT_BYTES):
        chunk = sock.recv(1)
        if not chunk:
            raise PingError("connection closed while reading a length")
        byte = chunk[0]
        value |= (byte & 0x7F) << (7 * i)
        if not byte & 0x80:
            return value
    raise PingError("VarInt too long -- not a Minecraft server")


def encode_string(text: str) -> bytes:
    raw = text.encode("utf-8")
    return encode_varint(len(raw)) + raw


def packet(packet_id: int, payload: bytes) -> bytes:
    body = encode_varint(packet_id) + payload
    return encode_varint(len(body)) + body


# ---------------------------------------------------------------------------------
# Result
# ---------------------------------------------------------------------------------

@dataclass
class ServerStatus:
    host: str
    port: int
    online: bool = False
    players_online: int = 0
    players_max: int = 0
    version: str = ""
    motd: str = ""
    latency_ms: int = 0
    error: str = ""
    checked_at: float = field(default_factory=time.time)

    @property
    def summary(self) -> str:
        if not self.online:
            return "offline"
        return f"{self.players_online}/{self.players_max} online"

    @property
    def compact(self) -> str:
        """A form short enough for a 268 px card: ``34.3k/200k``."""
        if not self.online:
            return "offline"

        def short(n: int) -> str:
            if n >= 1_000_000:
                return f"{n / 1_000_000:.1f}M".replace(".0M", "M")
            if n >= 1000:
                return f"{n / 1000:.1f}k".replace(".0k", "k")
            return str(n)

        return f"{short(self.players_online)}/{short(self.players_max)}"

    @property
    def detail(self) -> str:
        if not self.online:
            return self.error or "offline"
        bits = [f"{self.players_online}/{self.players_max} players"]
        if self.version:
            bits.append(self.version)
        if self.latency_ms:
            bits.append(f"{self.latency_ms} ms")
        return "  ·  ".join(bits)


_SECTION_CODES = re.compile("§.")


def _flatten_motd(node) -> str:
    """The description is either a string or a nested chat-component tree."""
    if isinstance(node, str):
        return node
    if isinstance(node, list):
        return "".join(_flatten_motd(n) for n in node)
    if isinstance(node, dict):
        text = str(node.get("text", ""))
        for child in node.get("extra", []) or []:
            text += _flatten_motd(child)
        return text
    return ""


def parse_address(address: str, default_port: int = DEFAULT_PORT) -> tuple[str, int]:
    """``host``, ``host:port`` -> (host, port). IPv6 literals use ``[::1]:25565``."""
    address = (address or "").strip()
    if address.startswith("["):
        host, _, rest = address[1:].partition("]")
        port = int(rest.lstrip(":") or default_port)
        return host, port
    host, sep, port_text = address.rpartition(":")
    if not sep:
        return address, default_port
    try:
        return host, int(port_text)
    except ValueError:
        return address, default_port


# ---------------------------------------------------------------------------------
# The ping
# ---------------------------------------------------------------------------------

def ping(address: str, *, timeout: float = 4.0) -> ServerStatus:
    """Query a server. Never raises -- failure is reported in the returned status."""
    host, port = parse_address(address)
    status = ServerStatus(host=host, port=port)
    started = time.monotonic()
    sock = None
    try:
        sock = socket.create_connection((host, port), timeout=timeout)
        sock.settimeout(timeout)

        # Handshake: protocol -1 ("unspecified"), then next state 1 = status.
        sock.sendall(packet(0x00,
                            encode_varint(-1)
                            + encode_string(host)
                            + struct.pack(">H", port)
                            + encode_varint(1)))
        sock.sendall(packet(0x00, b""))          # status request

        length = read_varint(sock)
        if length <= 0 or length > MAX_RESPONSE:
            raise PingError(f"implausible response length {length}")
        body = bytearray()
        while len(body) < length:
            chunk = sock.recv(min(4096, length - len(body)))
            if not chunk:
                raise PingError("connection closed mid-response")
            body += chunk

        # body = VarInt(packet id) || VarInt(json length) || json
        offset = 0

        def take_varint() -> int:
            nonlocal offset
            value = 0
            for i in range(MAX_VARINT_BYTES):
                byte = body[offset]
                offset += 1
                value |= (byte & 0x7F) << (7 * i)
                if not byte & 0x80:
                    return value
            raise PingError("VarInt too long in response")

        if take_varint() != 0x00:
            raise PingError("unexpected packet id in status response")
        json_len = take_varint()
        data = json.loads(bytes(body[offset:offset + json_len]).decode("utf-8", "replace"))

        players = data.get("players") or {}
        version = data.get("version") or {}
        status.online = True
        status.players_online = int(players.get("online", 0) or 0)
        status.players_max = int(players.get("max", 0) or 0)
        status.version = str(version.get("name", ""))[:40]
        status.motd = _SECTION_CODES.sub("", _flatten_motd(data.get("description")))[:120]
        status.latency_ms = int((time.monotonic() - started) * 1000)

    except (OSError, PingError, ValueError, IndexError, KeyError) as exc:
        status.online = False
        status.error = {
            ConnectionRefusedError: "not running",
            socket.timeout: "timed out",
            TimeoutError: "timed out",
            socket.gaierror: "unknown host",
        }.get(type(exc), str(exc)[:60] or "unreachable")
        log.debug("ping %s failed: %s", address, exc)
    finally:
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass
    return status


class StatusCache:
    """Ping results with a TTL, so a repaint never triggers a burst of network calls."""

    def __init__(self, ttl: float = 45.0) -> None:
        self.ttl = ttl
        self._entries: dict[str, ServerStatus] = {}
        self._pending: set[str] = set()

    def get(self, address: str) -> ServerStatus | None:
        entry = self._entries.get(address)
        if entry is None:
            return None
        if time.time() - entry.checked_at > self.ttl:
            return entry  # stale but shown; a refresh will replace it
        return entry

    def is_stale(self, address: str) -> bool:
        entry = self._entries.get(address)
        return entry is None or (time.time() - entry.checked_at) > self.ttl

    def claim(self, address: str) -> bool:
        """True if the caller should do the ping (nobody else is mid-flight)."""
        if address in self._pending:
            return False
        self._pending.add(address)
        return True

    def put(self, address: str, status: ServerStatus) -> None:
        self._entries[address] = status
        self._pending.discard(address)

    def release(self, address: str) -> None:
        self._pending.discard(address)
