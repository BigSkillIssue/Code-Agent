"""Run one shell hook: the event JSON on stdin, a time limit, and the exit code that decides.

Exit 0 lets things continue, exit 2 blocks (stderr is the reason), anything else is a
warning. Placeholders like `{path}` or `{tool}` in the command are filled from the event
(shell-quoted), so `ruff format {path}` works as a post_tool hook.
"""

import asyncio
import contextlib
import json
import os
import re
import shlex
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from forge.runtime.shell import find_shell, new_process_group, shell_env

HOOK_TIMEOUT_S = 30.0
PLACEHOLDER = re.compile(r"\{([a-z_]+)\}")


@dataclass
class HookRun:
    """What a hook command did."""

    code: int | None  # None = timed out
    stdout: str
    stderr: str


def expand(command: str, payload: dict[str, Any]) -> str:
    """Fill `{name}` from the event (tool arguments first), quoted for the shell."""
    args = payload.get("args") if isinstance(payload.get("args"), dict) else {}

    def value(match: re.Match[str]) -> str:
        key = match.group(1)
        found = args.get(key, payload.get(key)) if isinstance(args, dict) else payload.get(key)
        if found is None:
            return match.group(0)
        text = str(found)
        return text if sys.platform == "win32" else shlex.quote(text)

    return PLACEHOLDER.sub(value, command)


def hook_argv(command: str) -> list[str]:
    """bash -c where bash exists (Git Bash on Windows), else PowerShell."""
    bash = find_shell("bash")
    if bash is not None:
        return [bash, "-c", command]
    pwsh = find_shell("powershell") or "powershell"
    return [pwsh, "-NoProfile", "-NonInteractive", "-Command", command]


async def run_hook(
    command: str, payload: dict[str, Any], cwd: Path, timeout_s: float = HOOK_TIMEOUT_S
) -> HookRun:
    """Run the hook command; on timeout it is killed with its children."""
    proc = await asyncio.create_subprocess_exec(
        *hook_argv(expand(command, payload)),
        cwd=cwd,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=shell_env({"FORGE_HOOK_EVENT": str(payload.get("event", ""))}),
        **new_process_group(),  # type: ignore[arg-type]
    )
    data = json.dumps(payload, default=str).encode("utf-8")
    try:
        out, err = await asyncio.wait_for(proc.communicate(data), timeout_s)
    except TimeoutError:
        await kill_tree(proc)
        await proc.wait()
        return HookRun(None, "", f"timed out after {timeout_s:g}s")
    return HookRun(proc.returncode, out.decode("utf-8", "replace"), err.decode("utf-8", "replace"))


async def kill_tree(proc: asyncio.subprocess.Process) -> None:
    """Kill a hook and everything it started (children would keep its pipes open)."""
    if sys.platform == "win32":
        killer = await asyncio.create_subprocess_exec(
            "taskkill", "/PID", str(proc.pid), "/T", "/F",
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
        )  # fmt: skip
        await killer.wait()
        return
    with contextlib.suppress(ProcessLookupError):
        os.killpg(proc.pid, 9)
