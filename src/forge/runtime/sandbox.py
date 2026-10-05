"""OS sandboxes for shell commands: Landlock or bubblewrap (Linux), Seatbelt (macOS).

`workspace-write` lets commands write only inside the writable roots, the temp folders and
/dev; `read-only` only Forge's scratch folder and /dev; without `network` outgoing TCP is blocked.
Reads are never restricted. `full-access` runs commands unsandboxed. Windows has no OS
sandbox yet (see PROGRESS.md); there, only Forge's own path checks and approvals apply.
"""

import ctypes
import functools
import os
import shutil
import sys
import tempfile
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from forge.ports import SandboxPolicy

DENIED_MARKERS = (
    "read-only file system",
    "operation not permitted",
    "permission denied",
    "couldn't connect",
    "could not connect",
    "failed to connect",
    "network is unreachable",
    "could not resolve host",
)


@dataclass
class Launch:
    """How to start a process under a policy: an argv prefix and/or a pre-exec hook."""

    mechanism: str  # "landlock", "bubblewrap", "seatbelt" or "none"
    prefix: list[str] = field(default_factory=list)
    preexec: Callable[[], None] | None = None
    key: str = ""  # equal keys mean equal sandboxes (shells are reused only between those)
    env: dict[str, str] = field(default_factory=dict)  # extra environment for the process

    def argv(self, argv: list[str]) -> list[str]:
        """The command line to execute."""
        return [*self.prefix, *argv]


def launch_for(policy: SandboxPolicy) -> Launch:
    """The sandbox for a policy on this machine (mechanism 'none' when unavailable)."""
    if policy.mode == "full-access":
        return Launch("none", key="none")
    launch = os_launch(policy)
    if launch.mechanism != "none" and policy.mode == "read-only":
        # Shells need a temp folder (here-documents); the scratch folder is the writable one.
        scratch = os.path.realpath(scratch_dir())
        launch.env = {"TMPDIR": scratch, "TMP": scratch, "TEMP": scratch}
    return launch


def os_launch(policy: SandboxPolicy) -> Launch:
    """The OS mechanism for a restricted policy."""
    writable = writable_paths(policy)
    key = f"{policy.mode}|{policy.network}|{'|'.join(writable)}"
    if sys.platform == "darwin" and shutil.which("sandbox-exec"):
        profile = seatbelt_profile(writable, policy.network)
        return Launch("seatbelt", ["sandbox-exec", "-p", profile], key="seatbelt|" + key)
    if sys.platform.startswith("linux"):
        abi = landlock_abi()
        if abi >= 1 and (policy.network or abi >= 4):
            hook = functools.partial(apply_landlock, abi, writable, policy.network)
            return Launch("landlock", preexec=hook, key="landlock|" + key)
        if shutil.which("bwrap"):
            return Launch("bubblewrap", bwrap_prefix(writable, policy.network), key="bwrap|" + key)
    return Launch("none", key="none")


def writable_paths(policy: SandboxPolicy) -> list[str]:
    """Folders commands may write to: Forge's scratch folder and /dev always; with
    workspace-write also the writable roots and the system temp folders."""
    paths = [str(scratch_dir()), "/dev"]
    if policy.mode == "workspace-write":
        paths = [*policy.writable_roots, tempfile.gettempdir(), "/tmp", *paths]
    real = [os.path.realpath(p) for p in paths if os.path.exists(p)]
    return list(dict.fromkeys(real))


def scratch_dir() -> Path:
    """A private temp folder for Forge's own files (shell stderr captures)."""
    folder = Path(tempfile.gettempdir()) / "forge-scratch"
    folder.mkdir(exist_ok=True)
    return folder


def is_denied(policy: SandboxPolicy, exit_code: int | None, output: str) -> bool:
    """Heuristic: a failing command whose output reads like a sandbox refusal."""
    if policy.mode == "full-access" or exit_code in (0, None):
        return False
    lowered = output.lower()
    return any(marker in lowered for marker in DENIED_MARKERS)


# ----------------------------------------------------------------------------- macOS


DEVICES = '(literal "/dev/null") (regex #"^/dev/tty") (regex #"^/dev/fd/")'


def seatbelt_profile(writable: list[str], network: bool) -> str:
    """A Seatbelt profile: allow everything except writes outside `writable` (and network)."""
    subpaths = " ".join(f'(subpath "{escape(p)}")' for p in writable)
    lines = [
        "(version 1)",
        "(allow default)",
        "(deny file-write*)",
        f"(allow file-write* {subpaths} {DEVICES})",
    ]
    if not network:
        lines.append("(deny network*)")
    return "\n".join(lines)


def escape(path: str) -> str:
    """Quote a path for a Seatbelt string literal."""
    return path.replace("\\", "\\\\").replace('"', '\\"')


# ----------------------------------------------------------------------------- Linux: bubblewrap


def bwrap_prefix(writable: list[str], network: bool) -> list[str]:
    """bwrap arguments: the whole filesystem read-only, writable folders bound back read-write."""
    args = ["bwrap", "--ro-bind", "/", "/", "--dev", "/dev", "--proc", "/proc", "--die-with-parent"]
    for path in writable:
        if path != "/dev":
            args += ["--bind", path, path]
    if not network:
        args.append("--unshare-net")
    return [*args, "--"]


# ----------------------------------------------------------------------------- Linux: Landlock

SYS_CREATE_RULESET, SYS_ADD_RULE, SYS_RESTRICT_SELF = 444, 445, 446  # same on x86_64 and arm64
PR_SET_NO_NEW_PRIVS = 38
O_PATH = 0o10000000
O_CLOEXEC = 0o2000000  # Linux values; this code only runs on Linux
RULE_PATH_BENEATH = 1
NET_BIND_TCP, NET_CONNECT_TCP = 1, 2

# Write rights by the ABI version that introduced them (reads are never restricted).
FS_WRITE_RIGHTS = {
    1: (1 << 1)
    | (1 << 4)
    | (1 << 5)
    | (1 << 6)
    | (1 << 7)
    | (1 << 8)
    | (1 << 9)
    | (1 << 10)
    | (1 << 11)
    | (1 << 12),
    2: 1 << 13,  # REFER
    3: 1 << 14,  # TRUNCATE
    5: 1 << 15,  # IOCTL_DEV
}


class RulesetAttr(ctypes.Structure):
    """struct landlock_ruleset_attr."""

    _fields_ = [("handled_access_fs", ctypes.c_uint64), ("handled_access_net", ctypes.c_uint64)]


class PathBeneathAttr(ctypes.Structure):
    """struct landlock_path_beneath_attr (packed)."""

    _pack_ = 1
    _fields_ = [("allowed_access", ctypes.c_uint64), ("parent_fd", ctypes.c_int32)]


@functools.cache
def landlock_abi() -> int:
    """The kernel's Landlock ABI version, 0 when Landlock is not available."""
    if not sys.platform.startswith("linux"):
        return 0
    try:
        libc = ctypes.CDLL(None, use_errno=True)
        version = int(
            libc.syscall(SYS_CREATE_RULESET, None, 0, 1)
        )  # LANDLOCK_CREATE_RULESET_VERSION
    except (OSError, AttributeError):
        return 0
    return max(version, 0)


def write_rights(abi: int) -> int:
    """All filesystem write rights this ABI knows."""
    rights = 0
    for version, bits in FS_WRITE_RIGHTS.items():
        if abi >= version:
            rights |= bits
    return rights


def apply_landlock(abi: int, writable: list[str], network: bool) -> None:
    """Restrict the current process (runs in the child between fork and exec)."""
    libc = ctypes.CDLL(None, use_errno=True)
    rights = write_rights(abi)
    attr = RulesetAttr(rights, 0 if network or abi < 4 else NET_BIND_TCP | NET_CONNECT_TCP)
    size = ctypes.sizeof(RulesetAttr) if abi >= 4 else 8
    ruleset = libc.syscall(SYS_CREATE_RULESET, ctypes.byref(attr), size, 0)
    if ruleset < 0:
        raise OSError(ctypes.get_errno(), "landlock_create_ruleset failed")
    try:
        for path in writable:
            fd = os.open(path, O_PATH | O_CLOEXEC)
            try:
                rule = PathBeneathAttr(rights, fd)
                if (
                    libc.syscall(SYS_ADD_RULE, ruleset, RULE_PATH_BENEATH, ctypes.byref(rule), 0)
                    < 0
                ):
                    raise OSError(ctypes.get_errno(), f"landlock_add_rule failed for {path}")
            finally:
                os.close(fd)
        if libc.prctl(PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0) < 0:
            raise OSError(ctypes.get_errno(), "prctl(NO_NEW_PRIVS) failed")
        if libc.syscall(SYS_RESTRICT_SELF, ruleset, 0) < 0:
            raise OSError(ctypes.get_errno(), "landlock_restrict_self failed")
    finally:
        os.close(ruleset)
