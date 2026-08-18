"""Just enough NBT to write ``servers.dat``, so an instance arrives with its server saved.

Trap 12.3, and the detail that catches everyone: **servers.dat is uncompressed NBT.**
``level.dat`` is gzipped and the two look interchangeable, so the natural guess is wrong.
The structure is a root ``TAG_Compound`` with an *empty name*, containing a ``TAG_List``
called ``servers`` of compounds carrying the string tags ``ip`` and ``name``.

Everything written here is parsed straight back before it is trusted (:func:`save_servers`),
because a malformed servers.dat does not error -- it silently empties the server list, and
the user's saved servers are gone with no message.

This is a deliberately small subset: enough types to read a real servers.dat written by the
game (so an existing file can be merged rather than clobbered) and to write a valid one.
"""
from __future__ import annotations

import struct
from pathlib import Path
from typing import Any

from . import logs, net
from .paths import ext, mkdirs

log = logs.get("nbt")

TAG_END = 0
TAG_BYTE = 1
TAG_SHORT = 2
TAG_INT = 3
TAG_LONG = 4
TAG_FLOAT = 5
TAG_DOUBLE = 6
TAG_BYTE_ARRAY = 7
TAG_STRING = 8
TAG_LIST = 9
TAG_COMPOUND = 10
TAG_INT_ARRAY = 11
TAG_LONG_ARRAY = 12


class NbtError(ValueError):
    pass


# ---------------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------------

class _Reader:
    def __init__(self, data: bytes) -> None:
        self.data = data
        self.pos = 0

    def take(self, n: int) -> bytes:
        if self.pos + n > len(self.data):
            raise NbtError(f"truncated NBT: wanted {n} bytes at offset {self.pos}")
        out = self.data[self.pos:self.pos + n]
        self.pos += n
        return out

    def u1(self) -> int:
        return self.take(1)[0]

    def i2(self) -> int:
        return struct.unpack(">h", self.take(2))[0]

    def u2(self) -> int:
        return struct.unpack(">H", self.take(2))[0]

    def i4(self) -> int:
        return struct.unpack(">i", self.take(4))[0]

    def string(self) -> str:
        return self.take(self.u2()).decode("utf-8", "replace")

    def payload(self, tag: int) -> Any:
        if tag == TAG_BYTE:
            return struct.unpack(">b", self.take(1))[0]
        if tag == TAG_SHORT:
            return self.i2()
        if tag == TAG_INT:
            return self.i4()
        if tag == TAG_LONG:
            return struct.unpack(">q", self.take(8))[0]
        if tag == TAG_FLOAT:
            return struct.unpack(">f", self.take(4))[0]
        if tag == TAG_DOUBLE:
            return struct.unpack(">d", self.take(8))[0]
        if tag == TAG_BYTE_ARRAY:
            return self.take(self.i4())
        if tag == TAG_STRING:
            return self.string()
        if tag == TAG_LIST:
            item_tag = self.u1()
            count = self.i4()
            if count <= 0:
                return []
            return [self.payload(item_tag) for _ in range(count)]
        if tag == TAG_COMPOUND:
            out: dict[str, Any] = {}
            while True:
                child = self.u1()
                if child == TAG_END:
                    return out
                name = self.string()
                out[name] = self.payload(child)
        if tag == TAG_INT_ARRAY:
            return [self.i4() for _ in range(self.i4())]
        if tag == TAG_LONG_ARRAY:
            n = self.i4()
            return [struct.unpack(">q", self.take(8))[0] for _ in range(n)]
        raise NbtError(f"unsupported NBT tag {tag} at offset {self.pos - 1}")


def read_root(data: bytes) -> tuple[dict, int]:
    """Parse an uncompressed NBT document. Returns ``(root_compound, bytes_consumed)``."""
    if data[:2] == b"\x1f\x8b":
        raise NbtError(
            "This file is gzipped. servers.dat is uncompressed NBT -- level.dat is the "
            "gzipped one, and confusing the two is the usual cause.")
    r = _Reader(data)
    tag = r.u1()
    if tag != TAG_COMPOUND:
        raise NbtError(f"root tag must be TAG_Compound (10), got {tag}")
    r.string()  # the root name, which is empty in every real file
    return r.payload(TAG_COMPOUND), r.pos


# ---------------------------------------------------------------------------------
# Writing
# ---------------------------------------------------------------------------------

def _string(value: str) -> bytes:
    raw = value.encode("utf-8")
    if len(raw) > 0xFFFF:
        raise NbtError("NBT strings are limited to 65535 bytes")
    return struct.pack(">H", len(raw)) + raw


def _compound(fields: dict[str, str]) -> bytes:
    out = bytearray()
    for key, value in fields.items():
        out += bytes([TAG_STRING]) + _string(key) + _string(str(value))
    out += bytes([TAG_END])
    return bytes(out)


def write_servers_dat(servers: list[dict]) -> bytes:
    """Serialise a server list. Uncompressed, root compound with an empty name."""
    entries = [_compound({"ip": s.get("ip", ""), "name": s.get("name", "")})
               for s in servers]
    body = bytearray()
    body += bytes([TAG_LIST]) + _string("servers")
    body += bytes([TAG_COMPOUND]) + struct.pack(">i", len(entries))
    for e in entries:
        body += e
    body += bytes([TAG_END])
    return bytes([TAG_COMPOUND]) + _string("") + bytes(body)


def read_servers_dat(data: bytes) -> tuple[list[dict], int]:
    root, consumed = read_root(data)
    out = []
    for entry in root.get("servers") or []:
        if isinstance(entry, dict):
            out.append({"name": entry.get("name", ""), "ip": entry.get("ip", "")})
    return out, consumed


# ---------------------------------------------------------------------------------
# Files
# ---------------------------------------------------------------------------------

def load_servers(path: Path | str) -> list[dict]:
    try:
        with open(ext(Path(path)), "rb") as fh:
            data = fh.read()
    except OSError:
        return []
    if not data:
        return []
    try:
        return read_servers_dat(data)[0]
    except NbtError as exc:
        log.warning("could not read %s: %s -- leaving it alone", path, exc)
        raise


def save_servers(path: Path | str, servers: list[dict]) -> None:
    """Write, then parse back before trusting it. A bad file silently empties the list."""
    path = Path(path)
    blob = write_servers_dat(servers)
    parsed, consumed = read_servers_dat(blob)
    if parsed != [{"name": s.get("name", ""), "ip": s.get("ip", "")} for s in servers] \
            or consumed != len(blob):
        raise NbtError("refusing to write a servers.dat that does not parse back cleanly")
    mkdirs(path.parent)
    net.write_atomic(path, blob)


def add_server(path: Path | str, name: str, ip: str) -> list[dict]:
    """Append a server to an instance's list, merging rather than clobbering.

    An existing entry with the same address is left as it is -- the user may have renamed
    it, and overwriting their name would be rude.
    """
    path = Path(path)
    try:
        current = load_servers(path)
    except NbtError:
        return []  # unreadable: never destroy what we cannot understand
    if any(s.get("ip") == ip for s in current):
        return current
    current.append({"name": name, "ip": ip})
    save_servers(path, current)
    return current
