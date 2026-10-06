"""Persistent bash and PowerShell sessions: the only place with shell-specific syntax.

Each command is wrapped so that it runs in the right folder with stdin closed, its stderr
goes to a temp file, and a unique sentinel line reports its exit code and new folder.
"""

import asyncio
import base64
import codecs
import os
import re
import secrets
import subprocess
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from forge.runtime.proc import QUIET_ENV, which
from forge.runtime.sandbox import Launch, scratch_dir

ShellKind = Literal["bash", "powershell"]

GIT_BASH = Path("C:/Program Files/Git/bin/bash.exe")
BOMS = [
    (codecs.BOM_UTF8, "utf-8"),
    (codecs.BOM_UTF16_LE, "utf-16-le"),
    (codecs.BOM_UTF16_BE, "utf-16-be"),
]

POWERSHELL_INIT = (
    "[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false); "
    "$OutputEncoding = [Text.UTF8Encoding]::new($false); "
    "$ProgressPreference = 'SilentlyContinue'; "
    "if ($PSStyle) { $PSStyle.OutputRendering = 'PlainText' }"
)
_ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]|\x1b[=>]")
_MSYS_PATH = re.compile(r"^/([a-zA-Z])(/.*)?$")


class ShellExited(Exception):
    """The shell process ended (e.g. the command ran `exit`)."""


def find_shell(kind: ShellKind) -> str | None:
    """The executable for a shell kind, or None when it is not installed.

    $FORGE_BASH / $FORGE_POWERSHELL pick a specific executable, or `none` to act as if the
    shell were missing (CI tests Windows PowerShell 5.1, PowerShell 7 and Git Bash this way).
    """
    override = os.environ.get("FORGE_BASH" if kind == "bash" else "FORGE_POWERSHELL")
    if override:
        return None if override.lower() == "none" else which(override)
    if kind == "powershell":
        return which("pwsh") or (which("powershell.exe") if os.name == "nt" else None)
    if os.name == "nt":
        # C:\Windows\System32\bash.exe is WSL, which cannot use Windows paths; prefer Git Bash.
        if GIT_BASH.is_file():
            return str(GIT_BASH)
        found = which("bash")
        return found if found and "system32" not in found.lower() else None
    return which("bash")


def session_argv(kind: ShellKind, exe: str) -> list[str]:
    """Command line that starts a persistent shell reading commands from stdin."""
    if kind == "bash":
        return [exe, "--noprofile", "--norc"]
    return [
        exe,
        "-NoLogo",
        "-NoProfile",
        "-NonInteractive",
        "-ExecutionPolicy",
        "Bypass",
        "-Command",
        "-",
    ]


def one_shot_argv(kind: ShellKind, exe: str, script: str) -> list[str]:
    """Command line that runs one script in a fresh shell (background jobs)."""
    if kind == "bash":
        return [exe, "-c", script]
    return [*session_argv(kind, exe)[:-1], f"{POWERSHELL_INIT}; {script}"]


def shell_env(extra: Mapping[str, str] | None = None) -> dict[str, str]:
    """Environment for every command: no colours, no pagers, CI mode."""
    return {**os.environ, **QUIET_ENV, **(extra or {})}


def new_process_group() -> dict[str, int | bool]:
    """Keyword arguments that start a process in its own group, so it can be stopped as a tree."""
    if sys.platform == "win32":
        return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
    return {"start_new_session": True}


def _quote_bash(text: str) -> str:
    return "'" + text.replace("'", "'\\''") + "'"


def _quote_powershell(text: str) -> str:
    return "'" + text.replace("'", "''") + "'"


def wrap_bash(command: str, cwd: Path, nonce: str, err: Path) -> str:
    """The text sent to bash for one command: read literally from stdin up to a NUL byte.

    No here-document, because bash 3.2 (macOS) writes those to /tmp, which a read-only
    sandbox forbids; a command can never contain a NUL byte itself.
    """
    return (
        "IFS= read -r -d '' __FORGE_CMD\n"
        f"{command}\n\0"
        f'cd -- {_quote_bash(cwd.as_posix())} && {{ eval "$__FORGE_CMD"; }} '
        f"</dev/null 2>{_quote_bash(err.as_posix())}\n"
        f'printf \'\\n__FORGE_{nonce}__ %d %s\\n\' "$?" "$PWD"\n'
    )


def wrap_powershell(command: str, cwd: Path, nonce: str, err: Path) -> str:
    """The single line sent to PowerShell for one command (base64, so quoting cannot break it)."""
    encoded = base64.b64encode(command.encode("utf-8")).decode("ascii")
    return (
        f"$__forge_c = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('{encoded}')); "
        f"Set-Location -LiteralPath {_quote_powershell(str(cwd))}; "
        "$global:LASTEXITCODE = 0; $global:__forge_ok = $true; "
        ". { try { Invoke-Expression $__forge_c; $global:__forge_ok = $? } "
        "catch { Write-Error $_; $global:__forge_ok = $false } } "
        f"2> {_quote_powershell(str(err))} | Out-String -Width 4096 | "
        "ForEach-Object { [Console]::Out.Write($_) }; "
        "$__forge_code = if ($global:__forge_ok) { $LASTEXITCODE } "
        "elseif ($LASTEXITCODE) { $LASTEXITCODE } else { 1 }; "
        f'[Console]::Out.Write("`n__FORGE_{nonce}__ $__forge_code $((Get-Location).Path)`n")\n'
    )


def clean_output(raw: bytes) -> str:
    """Decode, drop terminal escape codes and use `\\n` line endings."""
    return _ANSI.sub("", decode_output(raw)).replace("\r\n", "\n")


def decode_output(raw: bytes) -> str:
    """Decode command output; Windows PowerShell 5.1 redirects stderr as UTF-16 with a BOM."""
    for bom, encoding in BOMS:
        if raw.startswith(bom):
            return raw[len(bom) :].decode(encoding, "replace")
    return raw.decode("utf-8", "replace")


def native_path(kind: ShellKind, reported: str) -> str:
    """Turn a Git Bash path like `/c/Users/me` back into `C:/Users/me` on Windows."""
    match = _MSYS_PATH.match(reported) if os.name == "nt" and kind == "bash" else None
    if match:
        return f"{match.group(1).upper()}:{match.group(2) or '/'}"
    return reported


@dataclass
class ShellOutcome:
    """What one command in a persistent shell produced."""

    exit_code: int | None
    stdout: str
    stderr: str = ""
    cwd: str | None = None
    timed_out: bool = False
    nonce: str = ""
    err_path: Path | None = None


class ShellSession:
    """One long-lived shell process that runs commands one at a time."""

    def __init__(
        self,
        kind: ShellKind,
        exe: str,
        env: Mapping[str, str] | None = None,
        launch: Launch | None = None,
    ) -> None:
        self.kind = kind
        self.exe = exe
        self.env = shell_env(env)
        self.launch = launch or Launch("none")
        self.proc: asyncio.subprocess.Process | None = None
        self.busy = False
        self._buffer = b""

    @property
    def alive(self) -> bool:
        """True while the shell process is running."""
        return self.proc is not None and self.proc.returncode is None

    async def start(self) -> None:
        """Start the shell process."""
        self.proc = await asyncio.create_subprocess_exec(
            *self.launch.argv(session_argv(self.kind, self.exe)),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            env=self.env,
            **new_process_group(),  # type: ignore[arg-type]
            preexec_fn=self.launch.preexec,
        )
        if self.kind == "powershell":
            await self._send(POWERSHELL_INIT + "\n")

    async def run(
        self,
        command: str,
        cwd: Path,
        timeout_s: float,
        on_output: Callable[[str], None] | None = None,
    ) -> ShellOutcome:
        """Run one command; on timeout the command keeps running and the outcome says so."""
        if not self.alive:
            await self.start()
        nonce = secrets.token_hex(8)
        err = scratch_dir() / f"forge-{nonce}.err"
        wrap = wrap_bash if self.kind == "bash" else wrap_powershell
        await self._send(wrap(command, cwd, nonce, err))
        parts: list[bytes] = []
        try:
            write = live_writer(parts, on_output)
            status = await asyncio.wait_for(self.stream_until(nonce, write), timeout_s)
        except TimeoutError:
            so_far = clean_output(b"".join(parts) + self._buffer)
            return ShellOutcome(None, so_far, timed_out=True, nonce=nonce, err_path=err)
        except ShellExited:
            code = await self.proc.wait() if self.proc else -1
            return ShellOutcome(code, self._finish_text(parts), take_text(err))
        code, reported = status
        cwd_after = native_path(self.kind, reported)
        return ShellOutcome(code, self._finish_text(parts), take_text(err), cwd_after)

    def _finish_text(self, parts: list[bytes]) -> str:
        text = clean_output(b"".join(parts))
        text = text[:-1] if text.endswith("\n") else text  # the newline the sentinel added
        return text.strip("\n") if self.kind == "powershell" else text

    async def stream_until(self, nonce: str, write: Callable[[bytes], object]) -> tuple[int, str]:
        """Pass output to `write` until the sentinel; returns (exit code, folder)."""
        marker = f"__FORGE_{nonce}__ ".encode()
        while True:
            index = self._buffer.find(marker)
            end = self._buffer.find(b"\n", index) if index != -1 else -1
            if end != -1:
                write(self._buffer[:index])
                status = self._buffer[index + len(marker) : end].decode("utf-8", "replace")
                self._buffer = self._buffer[end + 1 :]
                code, _, folder = status.strip().partition(" ")
                return int(code), folder
            if index == -1 and self._buffer:
                keep = split_marker(self._buffer, marker)  # the start of a marker still arriving
                write(self._buffer[: len(self._buffer) - keep])
                self._buffer = self._buffer[len(self._buffer) - keep :]
            assert self.proc is not None and self.proc.stdout is not None
            chunk = await self.proc.stdout.read(65536)
            if not chunk:
                write(self._buffer)
                self._buffer = b""
                raise ShellExited
            self._buffer += chunk

    async def close(self) -> None:
        """End the shell process: close its stdin, wait briefly, then kill it."""
        if self.proc is None:
            return
        if self.proc.stdin and not self.proc.stdin.is_closing():
            self.proc.stdin.close()
        try:
            await asyncio.wait_for(self.proc.wait(), 5)
        except TimeoutError:
            self.proc.kill()
            await self.proc.wait()

    async def _send(self, text: str) -> None:
        assert self.proc is not None and self.proc.stdin is not None
        self.proc.stdin.write(text.encode("utf-8"))
        await self.proc.stdin.drain()


def split_marker(buffer: bytes, marker: bytes) -> int:
    """Length of the longest end of `buffer` that is the start of `marker`."""
    for size in range(min(len(buffer), len(marker) - 1), 0, -1):
        if buffer.endswith(marker[:size]):
            return size
    return 0


def live_writer(
    parts: list[bytes], on_output: Callable[[str], None] | None
) -> Callable[[bytes], None]:
    """Collect output bytes; also pass them on as text while the command runs."""
    decoder = codecs.getincrementaldecoder("utf-8")("replace")

    def write(data: bytes) -> None:
        parts.append(data)
        if on_output is not None and data:
            text = decoder.decode(data)
            if text:
                on_output(_ANSI.sub("", text).replace("\r\n", "\n"))

    return write


def take_text(path: Path) -> str:
    """Read and delete a temp file of command stderr."""
    try:
        data = path.read_bytes()
    except FileNotFoundError:
        return ""
    path.unlink(missing_ok=True)
    return clean_output(data)
