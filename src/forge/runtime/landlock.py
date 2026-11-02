"""Landlock (Linux): restrict a command, then run it in the restricted process.

A sandboxed command starts as `forge __landlock <policy> -- <argv>`: this module restricts its
own process and replaces itself with the command. Doing it in a fresh interpreter, not in a
`preexec_fn` between fork and exec, keeps ctypes away from a forked copy of a process with
threads. Only the standard library is used here, so starting it costs little.

What is restricted: writes outside the writable folders; TCP unless the network is allowed (ABI
4); connecting to abstract Unix sockets outside the sandbox (ABI 6);
connecting to path Unix sockets outside the writable folders (ABI 9). Each feature is used only
when the kernel accepts it (a probe ruleset), so newer kernels get more and older ones keep working.
"""

import ctypes
import functools
import json
import os
import sys
from collections.abc import Sequence

SYS_CREATE_RULESET, SYS_ADD_RULE, SYS_RESTRICT_SELF = 444, 445, 446  # same on x86_64 and arm64
PR_SET_NO_NEW_PRIVS = 38
O_PATH = 0o10000000
O_CLOEXEC = 0o2000000  # Linux values; this code only runs on Linux
RULE_PATH_BENEATH = 1
NET_BIND_TCP, NET_CONNECT_TCP = 1, 2
SCOPE_ABSTRACT_UNIX = 1
RESOLVE_UNIX = 1 << 16  # LANDLOCK_ACCESS_FS_RESOLVE_UNIX (ABI 9)

# Write rights by the ABI version that introduced them (reads are never restricted).
FS_WRITE_RIGHTS = {
    1: (1 << 1) | (1 << 4) | (1 << 5) | (1 << 6) | (1 << 7) | (1 << 8) | (1 << 9) | (1 << 10)
    | (1 << 11) | (1 << 12),
    2: 1 << 13,  # REFER
    3: 1 << 14,  # TRUNCATE
    5: 1 << 15,  # IOCTL_DEV
}  # fmt: skip


class RulesetAttr(ctypes.Structure):
    """struct landlock_ruleset_attr (ABI 6 added `scoped`)."""

    _fields_ = [
        ("handled_access_fs", ctypes.c_uint64),
        ("handled_access_net", ctypes.c_uint64),
        ("scoped", ctypes.c_uint64),
    ]


class PathBeneathAttr(ctypes.Structure):
    """struct landlock_path_beneath_attr (packed)."""

    _pack_ = 1
    _fields_ = [("allowed_access", ctypes.c_uint64), ("parent_fd", ctypes.c_int32)]


def _libc() -> ctypes.CDLL:
    return ctypes.CDLL(None, use_errno=True)


@functools.cache
def abi() -> int:
    """The kernel's Landlock ABI version, 0 when Landlock is not available."""
    if not sys.platform.startswith("linux"):
        return 0
    try:
        version = int(_libc().syscall(SYS_CREATE_RULESET, None, 0, 1))  # ..._CREATE_RULESET_VERSION
    except (OSError, AttributeError):
        return 0
    return max(version, 0)


def write_rights(version: int) -> int:
    """All filesystem write rights this ABI knows."""
    rights = 0
    for since, bits in FS_WRITE_RIGHTS.items():
        if version >= since:
            rights |= bits
    return rights


@functools.cache
def accepted(fs: int, net: int, scoped: int) -> bool:
    """True if the kernel accepts a ruleset handling these rights (a probe, closed at once)."""
    attr = RulesetAttr(fs, net, scoped)
    size = ctypes.sizeof(RulesetAttr) if scoped else (16 if net else 8)
    try:
        fd = int(_libc().syscall(SYS_CREATE_RULESET, ctypes.byref(attr), size, 0))
    except (OSError, AttributeError):
        return False
    if fd < 0:
        return False
    os.close(fd)
    return True


def ruleset(version: int, network: bool) -> tuple[int, int, int]:
    """The (filesystem, network, scope) rights to handle on this kernel: everything it knows."""
    fs = write_rights(version)
    net = 0 if network or version < 4 else NET_BIND_TCP | NET_CONNECT_TCP
    scoped = SCOPE_ABSTRACT_UNIX if version >= 6 else 0  # signals stay: jobs stop each other
    if scoped and not accepted(fs, net, scoped):
        scoped = 0
    if version >= 9 and accepted(fs | RESOLVE_UNIX, net, scoped):
        fs |= RESOLVE_UNIX
    return fs, net, scoped


def restrict(writable: Sequence[str], network: bool, sockets: Sequence[str]) -> None:
    """Restrict this process: writes only below `writable`, Unix sockets only below `sockets`."""
    libc = _libc()
    fs, net, scoped = ruleset(abi(), network)
    attr = RulesetAttr(fs, net, scoped)
    size = ctypes.sizeof(RulesetAttr) if scoped else (16 if net else 8)
    handle = int(libc.syscall(SYS_CREATE_RULESET, ctypes.byref(attr), size, 0))
    if handle < 0:
        raise OSError(ctypes.get_errno(), "landlock_create_ruleset failed")
    try:
        for path in dict.fromkeys([*writable, *sockets]):
            rights = (fs & ~RESOLVE_UNIX if path in writable else 0) | (
                fs & RESOLVE_UNIX if path in sockets else 0
            )
            _allow(libc, handle, path, rights)
        if libc.prctl(PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0) < 0:
            raise OSError(ctypes.get_errno(), "prctl(NO_NEW_PRIVS) failed")
        if libc.syscall(SYS_RESTRICT_SELF, handle, 0) < 0:
            raise OSError(ctypes.get_errno(), "landlock_restrict_self failed")
    finally:
        os.close(handle)


def _allow(libc: ctypes.CDLL, handle: int, path: str, rights: int) -> None:
    """Add a path-beneath rule (skipped when there is nothing to allow)."""
    if not rights:
        return
    fd = os.open(path, O_PATH | O_CLOEXEC)
    try:
        rule = PathBeneathAttr(rights, fd)
        if libc.syscall(SYS_ADD_RULE, handle, RULE_PATH_BENEATH, ctypes.byref(rule), 0) < 0:
            raise OSError(ctypes.get_errno(), f"landlock_add_rule failed for {path}")
    finally:
        os.close(fd)


def main(args: Sequence[str]) -> int:
    """`<policy json> -- <argv>`: restrict this process, then become the command."""
    if len(args) < 3 or args[1] != "--":
        print("usage: forge __landlock <policy> -- <command> [args]", file=sys.stderr)
        return 2
    policy = json.loads(args[0])
    restrict(policy["writable"], bool(policy["network"]), policy.get("sockets", []))
    argv = list(args[2:])
    try:
        os.execvp(argv[0], argv)
    except OSError as exc:
        print(f"{argv[0]}: {exc.strerror}", file=sys.stderr)
        return 127
    return 0  # not reached: exec replaced the process
