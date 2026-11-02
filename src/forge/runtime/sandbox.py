"""OS sandboxes for shell commands: Landlock or bubblewrap (Linux), Seatbelt (macOS).

`workspace-write` lets commands write only inside the writable roots, the temp folders and
/dev; `read-only` only Forge's scratch folder and /dev; without `network` outgoing TCP is blocked.
Reads are never restricted. `full-access` runs commands unsandboxed. Windows has no OS
sandbox yet (see PROGRESS.md); there, only Forge's own path checks and approvals apply.

Inside a writable root, the settings that make git or Forge run programs (`.git/config`,
`.git/hooks`, `.git/info`, `.forge/config.toml`, `.forge/hooks`) stay read-only under bubblewrap
and Seatbelt; Landlock cannot carve them out. Sockets that lead out of the sandbox (the user's
session bus, X11, Docker) are hidden or refused as far as each mechanism allows.
"""

import atexit
import functools
import json
import os
import shutil
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from forge.ports import SandboxPolicy
from forge.runtime import landlock

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
# Relative to each writable root: settings that run programs when git or Forge reads them.
PROTECTED = (".git/config", ".git/hooks", ".git/info", ".forge/config.toml", ".forge/hooks")
# Sockets of other programs that would let a command act outside the sandbox.
SOCKET_FOLDERS = ("/tmp/.X11-unix",)
SOCKET_FILES = ("/run/docker.sock", "/var/run/docker.sock", "/run/podman/podman.sock")


@dataclass
class Launch:
    """How to start a process under a policy: an argv prefix and extra environment."""

    mechanism: str  # "landlock", "bubblewrap", "seatbelt" or "none"
    prefix: list[str] = field(default_factory=list)
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
    protected = protected_paths(policy)
    key = f"{policy.mode}|{policy.network}|{'|'.join(writable)}"
    if sys.platform == "darwin" and shutil.which("sandbox-exec"):
        profile = seatbelt_profile(writable, policy.network, protected)
        return Launch("seatbelt", ["sandbox-exec", "-p", profile], key="seatbelt|" + key)
    if sys.platform.startswith("linux"):
        abi = landlock.abi()
        if abi >= 1 and (policy.network or abi >= 4):
            return Launch("landlock", landlock_prefix(policy, writable), key="landlock|" + key)
        if shutil.which("bwrap"):
            prefix = bwrap_prefix(writable, policy.network, protected)
            return Launch("bubblewrap", prefix, key="bwrap|" + key)
    return Launch("none", key="none")


def writable_paths(policy: SandboxPolicy) -> list[str]:
    """Folders commands may write to: Forge's scratch folder and /dev always; with
    workspace-write also the writable roots and the system temp folders."""
    paths = [str(scratch_dir()), "/dev"]
    if policy.mode == "workspace-write":
        paths = [*policy.writable_roots, tempfile.gettempdir(), "/tmp", *paths]
    real = [os.path.realpath(p) for p in paths if os.path.exists(p)]
    return list(dict.fromkeys(real))


def protected_paths(policy: SandboxPolicy) -> list[str]:
    """The settings files and folders inside the writable roots that commands may not change."""
    if policy.mode != "workspace-write":
        return []
    roots = [os.path.realpath(root) for root in policy.writable_roots]
    return [os.path.join(root, *rel.split("/")) for root in roots for rel in PROTECTED]


def socket_paths(policy: SandboxPolicy) -> list[str]:
    """Where Landlock (ABI 9) lets commands reach Unix sockets: the writable roots and the
    scratch folder, not the shared temp folders (X11, other programs' agents)."""
    roots = policy.writable_roots if policy.mode == "workspace-write" else []
    paths = [*roots, str(scratch_dir())]
    return list(dict.fromkeys(os.path.realpath(p) for p in paths if os.path.exists(p)))


@functools.cache
def scratch_dir() -> Path:
    """This Forge process's private temp folder (mode 0700) for its own files: shell stderr
    captures and the local Postgres; removed when Forge exits."""
    folder = Path(tempfile.mkdtemp(prefix="forge-scratch-"))
    atexit.register(shutil.rmtree, folder, ignore_errors=True)
    return folder


def is_denied(policy: SandboxPolicy, exit_code: int | None, output: str) -> bool:
    """Heuristic: a failing command whose output reads like a sandbox refusal."""
    if policy.mode == "full-access" or exit_code in (0, None):
        return False
    lowered = output.lower()
    return any(marker in lowered for marker in DENIED_MARKERS)


# ----------------------------------------------------------------------------- macOS


DEVICES = '(literal "/dev/null") (regex #"^/dev/tty") (regex #"^/dev/fd/")'


# Programs that hand work to processes outside the sandbox (launchd jobs, other apps).
MAC_DELEGATES = ("/bin/launchctl", "/usr/bin/osascript", "/usr/bin/open", "/usr/bin/shortcuts")


def seatbelt_profile(writable: list[str], network: bool, protected: list[str] | None = None) -> str:
    """A Seatbelt profile: allow everything except writes outside `writable` (and network),
    writes to the protected settings, Apple events and programs that start work elsewhere."""
    lines = [
        "(version 1)",
        "(allow default)",
        "(deny file-write*)",
        f"(allow file-write* {filters('subpath', writable)} {DEVICES})",
    ]
    if protected:  # later rules win, so these beat the writable roots they lie in
        lines.append(f"(deny file-write* {filters('subpath', protected)})")
    lines.append("(deny appleevent-send)")
    lines.append(f"(deny process-exec {filters('literal', list(MAC_DELEGATES))})")
    if not network:
        lines.append("(deny network*)")
    return "\n".join(lines)


def filters(kind: str, paths: list[str]) -> str:
    """`(subpath "/a") (subpath "/b")` for a list of paths."""
    return " ".join(f'({kind} "{escape(p)}")' for p in paths)


def escape(path: str) -> str:
    """Quote a path for a Seatbelt string literal."""
    return path.replace("\\", "\\\\").replace('"', '\\"')


# ----------------------------------------------------------------------------- Linux: bubblewrap


def bwrap_prefix(
    writable: list[str], network: bool, protected: list[str] | None = None
) -> list[str]:
    """bwrap arguments: the whole filesystem read-only, writable folders bound back read-write,
    the protected settings read-only again, other programs' sockets hidden, own process and IPC
    namespaces and a new session (no keystrokes into the user's terminal)."""
    args = ["bwrap", "--ro-bind", "/", "/", "--dev", "/dev", "--proc", "/proc"]
    args += ["--unshare-pid", "--unshare-ipc", "--new-session", "--die-with-parent"]
    for path in writable:
        if path != "/dev":
            args += ["--bind", path, path]
    for path in protected or []:
        args += ["--ro-bind-try", path, path]
    for folder in hidden_socket_folders(writable):
        args += ["--tmpfs", folder]
    for socket in dict.fromkeys(os.path.realpath(p) for p in SOCKET_FILES if os.path.exists(p)):
        args += ["--ro-bind", "/dev/null", socket]
    if not network:
        args.append("--unshare-net")
    return [*args, "--"]


def hidden_socket_folders(writable: list[str]) -> list[str]:
    """Folders of other programs' sockets (the user's runtime folder, X11) that exist and hold
    no writable root."""
    folders = [*SOCKET_FOLDERS]
    if hasattr(os, "getuid"):
        folders.append(f"/run/user/{os.getuid()}")
    return [
        folder
        for folder in folders
        if os.path.isdir(folder) and not any(Path(p).is_relative_to(folder) for p in writable)
    ]


# ----------------------------------------------------------------------------- Linux: Landlock


def landlock_prefix(policy: SandboxPolicy, writable: list[str]) -> list[str]:
    """The command line that restricts itself with Landlock and then runs the command."""
    rules = {"writable": writable, "network": policy.network, "sockets": socket_paths(policy)}
    return [*forge_entry(), "__landlock", json.dumps(rules), "--"]


def forge_entry() -> list[str]:
    """How to start Forge itself: the single-file binary, or this Python with the package."""
    if getattr(sys, "frozen", False):
        return [sys.executable]
    return [sys.executable, "-I", "-m", "forge"]


# ----------------------------------------------------------------------------- Linux: Landlock
