"""Which TCP ports programs in the sandbox listen on (for the live preview).

Linux (every container) reads /proc; macOS and the BSDs (local mode) ask `lsof` and `ps`.
"""

import os
import subprocess
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
        inode = fields[9] if len(fields) > 9 else ""
        found.append(
            {"port": int(port_hex, 16), "address": decode_address(address, family), "inode": inode}
        )
    return found


def decode_address(hex_address: str, family: str) -> str:
    """127.0.0.1 or :: style text for a little-endian hex address from /proc."""
    raw = bytes.fromhex(hex_address)
    if family == "ipv4":
        return ".".join(str(b) for b in reversed(raw))
    words = [raw[i : i + 4][::-1] for i in range(0, 16, 4)]
    groups = [b"".join(words)[i : i + 2].hex() for i in range(0, 16, 2)]
    return ":".join(g.lstrip("0") or "0" for g in groups)


def listening_ports(
    exclude: set[int] | frozenset[int] = frozenset(), owned_by: int | None = None
) -> list[dict[str, Any]]:
    """Every listening TCP port (Linux, macOS, BSD; empty elsewhere), without the `exclude`d
    ones; with `owned_by`, only those of that process and the processes below it."""
    if sys.platform.startswith("linux"):
        return from_proc(exclude, owned_by)
    if sys.platform == "darwin" or "bsd" in sys.platform:
        return from_lsof(exclude, owned_by)
    return []


def from_proc(exclude: set[int] | frozenset[int], owned_by: int | None) -> list[dict[str, Any]]:
    """Listening ports from /proc (Linux)."""
    inodes = socket_inodes(descendants(owned_by)) if owned_by is not None else None
    found: dict[int, dict[str, Any]] = {}
    for path, family in zip(PROC_FILES, ("ipv4", "ipv6"), strict=True):
        try:
            text = path.read_text(encoding="ascii")
        except OSError:
            continue
        for entry in parse_proc_net(text, family):
            inode = entry.pop("inode")
            if entry["port"] not in exclude and (inodes is None or inode in inodes):
                found.setdefault(entry["port"], entry)
    return sorted(found.values(), key=lambda e: e["port"])


def from_lsof(exclude: set[int] | frozenset[int], owned_by: int | None) -> list[dict[str, Any]]:
    """Listening ports from `lsof` (macOS, BSD)."""
    listing = run_quietly(["lsof", "-nP", "-iTCP", "-sTCP:LISTEN", "-Fpn"])
    pids = None
    if owned_by is not None:
        tree = ps_children(run_quietly(["ps", "-A", "-o", "pid=", "-o", "ppid="]))
        pids = descendants(owned_by, tree)
    found: dict[int, dict[str, Any]] = {}
    for entry in parse_lsof(listing):
        pid = entry.pop("pid")
        if entry["port"] not in exclude and (pids is None or pid in pids):
            found.setdefault(entry["port"], entry)
    return sorted(found.values(), key=lambda e: e["port"])


def run_quietly(argv: list[str]) -> str:
    """A command's output, or "" when it is missing or fails to finish."""
    try:
        done = subprocess.run(argv, capture_output=True, text=True, timeout=10, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return done.stdout


def parse_lsof(text: str) -> list[dict[str, Any]]:
    """Listening sockets in `lsof -F pn` output: `p<pid>` lines, then `n<address>:<port>`."""
    found: list[dict[str, Any]] = []
    pid = -1
    for line in text.splitlines():
        if line.startswith("p") and line[1:].isdigit():
            pid = int(line[1:])
        elif line.startswith("n"):
            address, _, port = line[1:].rpartition(":")
            if port.isdigit() and address:
                address = "0.0.0.0" if address == "*" else address.strip("[]")
                found.append({"port": int(port), "address": address, "pid": pid})
    return found


def ps_children(text: str) -> dict[int, list[int]]:
    """Parent pid -> child pids from `ps -o pid= -o ppid=` output."""
    children: dict[int, list[int]] = {}
    for line in text.splitlines():
        fields = line.split()
        if len(fields) == 2 and fields[0].isdigit() and fields[1].isdigit():
            children.setdefault(int(fields[1]), []).append(int(fields[0]))
    return children


def proc_children() -> dict[int, list[int]]:
    """Parent pid -> child pids, from /proc."""
    children: dict[int, list[int]] = {}
    for stat in Path("/proc").glob("[0-9]*/stat"):
        try:
            after_name = stat.read_text(encoding="utf-8", errors="replace").rsplit(")", 1)[1]
            children.setdefault(int(after_name.split()[1]), []).append(int(stat.parent.name))
        except (OSError, IndexError, ValueError):
            continue  # the process ended meanwhile
    return children


def descendants(root: int, children: dict[int, list[int]] | None = None) -> set[int]:
    """`root` and every process below it (from /proc unless `children` is given)."""
    if children is None:
        children = proc_children()
    found: set[int] = set()
    todo = [root]
    while todo:
        pid = todo.pop()
        if pid not in found:
            found.add(pid)
            todo.extend(children.get(pid, []))
    return found


def socket_inodes(pids: set[int]) -> set[str]:
    """The inodes of every socket these processes hold open."""
    inodes = set()
    for pid in pids:
        try:
            fds = os.listdir(f"/proc/{pid}/fd")
        except OSError:
            continue
        for fd in fds:
            try:
                link = os.readlink(f"/proc/{pid}/fd/{fd}")
            except OSError:
                continue
            if link.startswith("socket:["):
                inodes.add(link[len("socket:[") : -1])
    return inodes
