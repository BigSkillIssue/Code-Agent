"""Programs the daemon runs for the user (dev servers, one-off commands), with their output."""

import asyncio
import os
import secrets
import shutil
import sys
import time
from collections import deque
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from forge.local.local_executor import stop_tree
from forge.runtime.shell import new_process_group

from forge_sandbox.fsops import Owner, Workspace, split_path
from forge_sandbox.methods import ProcOutputParams, ProcParams, ProcStartParams, method
from forge_sandbox.rpc import Handler, RpcError

MAX_PROCS = 32
KEEP_LINES = 5000
MAX_LINE = 4000
Notify = Callable[[str, dict[str, Any]], Awaitable[None]]


def owner_kwargs(owner: Owner | None) -> dict[str, Any]:
    """subprocess arguments that run a child as the workspace owner (when we are root)."""
    if owner is None or sys.platform == "win32" or os.geteuid() != 0:
        return {}
    return {"user": owner.uid, "group": owner.gid, "extra_groups": []}


def spawn_options(owner: Owner | None) -> dict[str, Any]:
    """subprocess arguments for a child: its own process group, run as the workspace owner."""
    return {**owner_kwargs(owner), **new_process_group()}


def shell_argv(command: str) -> list[str]:
    """argv that runs a command line in the platform's shell."""
    if sys.platform == "win32":
        return ["cmd.exe", "/d", "/c", command]
    return [shutil.which("bash") or "/bin/sh", "-c", command]


def workspace_dir(workspace: Workspace, rel: str) -> Path:
    """A directory inside the workspace to start a program in."""
    path = workspace.root.joinpath(*split_path(rel)).resolve()
    if path != workspace.root and workspace.root not in path.parents:
        raise RpcError("invalid_path", f"{rel} leads outside the workspace")
    if not path.is_dir():
        raise RpcError("not_found", f"{rel or '.'} is not a directory")
    return path


async def run_program(
    argv: Sequence[str],
    cwd: Path,
    *,
    owner: Owner | None,
    env: Mapping[str, str],
    timeout: float = 60,
) -> tuple[int, bytes, bytes]:
    """Run a program to completion as the workspace owner; code -1 on timeout."""
    proc = await asyncio.create_subprocess_exec(
        argv[0],
        *argv[1:],
        cwd=cwd,
        env=dict(env),
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        **spawn_options(owner),
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout)
    except TimeoutError:
        await stop_tree(proc)
        return -1, b"", f"timed out after {timeout:.0f}s".encode()
    return proc.returncode if proc.returncode is not None else -1, out, err


@dataclass
class Proc:
    """One program the user started."""

    id: str
    name: str
    argv: list[str]
    cwd: str
    started_at: float
    process: asyncio.subprocess.Process
    lines: deque[str] = field(default_factory=lambda: deque(maxlen=KEEP_LINES))
    total_lines: int = 0
    exit_code: int | None = None

    def add(self, raw: bytes) -> None:
        """Keep one line of output."""
        self.lines.append(raw.decode("utf-8", "replace").rstrip("\r")[:MAX_LINE])
        self.total_lines += 1

    def info(self) -> dict[str, Any]:
        """What procs.list reports."""
        return {
            "id": self.id,
            "name": self.name,
            "argv": self.argv,
            "cwd": self.cwd,
            "started_at": self.started_at,
            "pid": self.process.pid,
            "running": self.exit_code is None,
            "exit_code": self.exit_code,
            "total_lines": self.total_lines,
        }


class Procs:
    """Starts, lists, reads and stops the user's programs."""

    def __init__(
        self, workspace: Workspace, env: Mapping[str, str], notify: Notify | None = None
    ) -> None:
        self.workspace = workspace
        self.env = dict(env)
        self.notify = notify
        self.procs: dict[str, Proc] = {}
        self._readers: set[asyncio.Task[None]] = set()

    def handlers(self) -> dict[str, Handler]:
        """procs.* methods."""
        return {
            "procs.start": method(ProcStartParams, self.start),
            "procs.list": self.list,
            "procs.output": method(ProcOutputParams, self.output),
            "procs.stop": method(ProcParams, self.stop),
        }

    async def start(self, params: ProcStartParams) -> dict[str, Any]:
        """Start a program; its output is kept line by line."""
        if (params.argv is None) == (params.command is None):
            raise RpcError("bad_params", "give either argv or command")
        if sum(p.exit_code is None for p in self.procs.values()) >= MAX_PROCS:
            raise RpcError("too_many", f"at most {MAX_PROCS} programs may run at once")
        argv = params.argv if params.argv is not None else shell_argv(params.command or "")
        if not argv:
            raise RpcError("bad_params", "argv is empty")
        cwd = workspace_dir(self.workspace, params.cwd)
        try:
            process = await asyncio.create_subprocess_exec(
                argv[0],
                *argv[1:],
                cwd=cwd,
                env={**self.env, **params.env},
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                **spawn_options(self.workspace.owner),
            )
        except OSError as err:
            raise RpcError("start_failed", f"could not start {argv[0]}: {err.strerror}") from None
        proc = Proc(
            id=f"p{secrets.token_hex(4)}",
            name=params.name or " ".join(argv)[:120],
            argv=list(argv),
            cwd=params.cwd,
            started_at=time.time(),
            process=process,
        )
        self.procs[proc.id] = proc
        task = asyncio.create_task(self._collect(proc))
        self._readers.add(task)
        task.add_done_callback(self._readers.discard)
        return proc.info()

    async def _collect(self, proc: Proc) -> None:
        assert proc.process.stdout is not None
        pending = b""
        while chunk := await proc.process.stdout.read(65536):
            *lines, pending = (pending + chunk).split(b"\n")
            if len(pending) > MAX_LINE:  # a "line" without end (progress bars): cut it
                lines.append(pending)
                pending = b""
            for line in lines:
                proc.add(line)
        if pending:
            proc.add(pending)
        proc.exit_code = await proc.process.wait()
        if self.notify is not None:
            await self.notify("procs.exited", {"id": proc.id, "exit_code": proc.exit_code})

    async def list(self, _params: dict[str, Any]) -> list[dict[str, Any]]:
        """Every program, newest first."""
        return [p.info() for p in sorted(self.procs.values(), key=lambda p: -p.started_at)]

    async def output(self, params: ProcOutputParams) -> dict[str, Any]:
        """Lines from line number `since` on (older lines may have been dropped)."""
        proc = self._get(params.id)
        first_kept = proc.total_lines - len(proc.lines)
        start = max(params.since, first_kept)
        kept = list(proc.lines)[start - first_kept : start - first_kept + params.limit]
        return {"lines": kept, "from": start, "next": start + len(kept), **proc.info()}

    async def stop(self, params: ProcParams) -> dict[str, Any]:
        """Stop a program and its children."""
        proc = self._get(params.id)
        how = await stop_tree(proc.process)
        return {"stopped": how, **proc.info()}

    def _get(self, proc_id: str) -> Proc:
        proc = self.procs.get(proc_id)
        if proc is None:
            raise RpcError("not_found", f"no program {proc_id}")
        return proc

    async def close(self) -> None:
        """Stop every running program."""
        for proc in self.procs.values():
            if proc.exit_code is None:
                await stop_tree(proc.process)
        for task in self._readers:
            task.cancel()
