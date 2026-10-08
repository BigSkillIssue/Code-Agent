"""Which TCP ports programs in the sandbox listen on (for the live preview)."""

import sys
from pathlib import Path
from typing import Any

LISTEN = "0A"
PROC_FILES = (Path("/proc/net/tcp"), Path("/proc/net/tcp6"))


def parse_proc_net(text: str, family: str) -> list[dict[str, Any]]:
    """Listening sockets in one /proc/net/tcp-style table."""
    found = []
    for line in text.splitlines()[1:]:
        fields = line.split()
        if len(fields) < 4 or fields[3] != LISTEN:
            continue
        address, _, port_hex = fields[1].partition(":")
        found.append({"port": int(port_hex, 16), "address": decode_address(address, family)})
    return found


def decode_address(hex_address: str, family: str) -> str:
    """127.0.0.1 or :: style text for a little-endian hex address from /proc."""
    raw = bytes.fromhex(hex_address)
    if family == "ipv4":
        return ".".join(str(b) for b in reversed(raw))
    words = [raw[i : i + 4][::-1] for i in range(0, 16, 4)]
    groups = [b"".join(words)[i : i + 2].hex() for i in range(0, 16, 2)]
    return ":".join(g.lstrip("0") or "0" for g in groups)


def listening_ports(exclude: set[int] | frozenset[int] = frozenset()) -> list[dict[str, Any]]:
    """Every listening TCP port (Linux; empty elsewhere), without the `exclude`d ones."""
    if not sys.platform.startswith("linux"):
        return []
    found: dict[int, dict[str, Any]] = {}
    for path, family in zip(PROC_FILES, ("ipv4", "ipv6"), strict=True):
        try:
            text = path.read_text(encoding="ascii")
        except OSError:
            continue
        for entry in parse_proc_net(text, family):
            if entry["port"] not in exclude:
                found.setdefault(entry["port"], entry)
    return sorted(found.values(), key=lambda e: e["port"])
