"""LocalExecutor: runs commands on this machine in persistent shells or as background jobs.

The sandbox policy is recorded but not yet enforced (S28 adds the OS sandboxes).
"""

import asyncio
import contextlib
import os
import signal
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from forge.ports import Command, CommandResult, JobNotFoundError, SandboxPolicy
from forge.runtime.proc import run_argv
from forge.runtime.shell import (
    ShellExited,
    ShellKind,
    ShellOutcome,
    ShellSession,
    clean_output,
    find_shell,
    new_process_group,
    one_shot_argv,
    shell_env,
    take_text,
)

STOP_GRACE_S = 5.0
LAST_LINES = 20


@dataclass
class Job:
    """A background process and the log file its output goes to."""

    id: str
    proc: asyncio.subprocess.Process
    log_path: Path
    started: float
    exit_code: int | None = None
    ended: float | None = None
    task: "asyncio.Task[None] | None" = None

    def elapsed(self) -> float:
        """Seconds the job ran so far (or in total, once it ended)."""
        return (self.ended or time.time()) - self.started


class LocalExecutor:
    """Runs commands locally: scripts in pooled persistent shells, jobs with log files."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.jobs: dict[str, Job] = {}
        self.last_policy: SandboxPolicy | None = None
        self._shells: list[ShellSession] = []

    async def run(
        self, cmd: Command, policy: SandboxPolicy, background: bool = False
    ) -> CommandResult:
        """Run a command now, or start it as a background job."""
        self.last_policy = policy
        if background:
            return await self._start_job(cmd)
        if cmd.script is not None and cmd.shell != "none" and not cmd.env:
            return await self._run_in_shell(cmd, cmd.shell)
        return await self._run_once(cmd)

    async def job_output(self, job_id: str, since_line: int = 0) -> CommandResult:
        """Log lines of a job from `since_line` (0-based); exit_code is None while running."""
        job = self._job(job_id)
        lines = self._log_lines(job)
        return self._job_result(job, "\n".join(lines[since_line:]), len(lines))

    async def job_stop(self, job_id: str) -> CommandResult:
        """Stop a job and its children; stderr says how (`SIGTERM`, `SIGKILL`, ...)."""
        job = self._job(job_id)
        how = "already exited"
        if job.ended is None:
            how = await stop_tree(job.proc)
            if job.task is not None:
                await asyncio.wait({job.task}, timeout=STOP_GRACE_S)
        lines = self._log_lines(job)
        result = self._job_result(job, "\n".join(lines[-LAST_LINES:]), len(lines))
        return result.model_copy(update={"stderr": how})

    async def close(self) -> None:
        """Stop every running job and end every shell (at session end)."""
        for job in self.jobs.values():
            if job.ended is None:
                await stop_tree(job.proc)
        for shell in self._shells:
            if shell.busy and shell.proc is not None:
                await stop_tree(shell.proc)  # interrupted mid-command: stop it and its children
            else:
                await shell.close()
        self._shells.clear()

    async def _run_in_shell(self, cmd: Command, kind: ShellKind) -> CommandResult:
        exe = find_shell(kind)
        if exe is None:
            return CommandResult(exit_code=127, stdout="", stderr=f"{kind} is not installed")
        shell = await self._borrow(kind, exe)
        script = cmd.script or ""
        outcome = await shell.run(script, Path(cmd.cwd), cmd.timeout_s)
        if outcome.timed_out:
            self._shells.remove(shell)
            if script.lstrip().startswith("sleep") and shell.proc is not None:
                # Waiting is the whole point of sleep, so a background copy is useless.
                await stop_tree(shell.proc)
                return CommandResult(
                    exit_code=None, stdout=outcome.stdout, stderr="", timed_out=True
                )
            job = self._adopt(shell, outcome)
            return CommandResult(
                exit_code=None,
                stdout=outcome.stdout,
                stderr="",
                timed_out=True,
                job_id=job.id,
                pid=job.proc.pid,
            )
        shell.busy = False
        if not shell.alive:
            self._shells.remove(shell)
        return CommandResult(
            exit_code=outcome.exit_code,
            stdout=outcome.stdout,
            stderr=outcome.stderr,
            cwd=outcome.cwd,
        )

    async def _borrow(self, kind: ShellKind, exe: str) -> ShellSession:
        for shell in self._shells:
            if shell.kind == kind and not shell.busy and shell.alive:
                shell.busy = True
                return shell
        shell = ShellSession(kind, exe)
        await shell.start()
        shell.busy = True
        self._shells.append(shell)
        return shell

    def _adopt(self, shell: ShellSession, outcome: ShellOutcome) -> Job:
        """Turn a timed-out shell into a background job; its output keeps going to the log."""
        assert shell.proc is not None
        job = self._register(self._next_id(), shell.proc)
        job.task = asyncio.create_task(self._pump(job, shell, outcome))
        return job

    async def _pump(self, job: Job, shell: ShellSession, outcome: ShellOutcome) -> None:
        with job.log_path.open("ab") as log:
            log.write(outcome.stdout.encode("utf-8"))

            def write(data: bytes) -> None:
                log.write(data)
                log.flush()

            try:
                code, _ = await shell.stream_until(outcome.nonce, write)
            except ShellExited:
                code = await job.proc.wait()
            if outcome.err_path is not None:
                log.write(take_text(outcome.err_path).encode("utf-8"))
        job.exit_code, job.ended = code, time.time()
        await shell.close()

    async def _start_job(self, cmd: Command) -> CommandResult:
        argv = self._argv(cmd)
        if argv is None:
            return CommandResult(exit_code=127, stdout="", stderr=f"{cmd.shell} is not installed")
        job_id = self._next_id()
        log_path = self._log_path(job_id)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("ab") as log:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                cwd=cmd.cwd,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=log,
                stderr=asyncio.subprocess.STDOUT,
                env=shell_env(cmd.env),
                **new_process_group(),  # type: ignore[arg-type]
            )
        job = self._register(job_id, proc)
        job.task = asyncio.create_task(self._watch(job))
        return CommandResult(exit_code=None, stdout="", stderr="", job_id=job.id, pid=proc.pid)

    async def _watch(self, job: Job) -> None:
        job.exit_code = await job.proc.wait()
        job.ended = time.time()

    async def _run_once(self, cmd: Command) -> CommandResult:
        argv = self._argv(cmd)
        if argv is None:
            return CommandResult(exit_code=127, stdout="", stderr=f"{cmd.shell} is not installed")
        result = await run_argv(argv, Path(cmd.cwd), timeout_s=cmd.timeout_s, env=cmd.env)
        timed_out = result.code == -1 and result.stderr.startswith("timed out")
        return CommandResult(
            exit_code=None if timed_out else result.code,
            stdout=clean_output(result.stdout.encode()),
            stderr=clean_output(result.stderr.encode()),
            timed_out=timed_out,
        )

    def _argv(self, cmd: Command) -> list[str] | None:
        if cmd.script is not None and cmd.shell != "none":
            exe = find_shell(cmd.shell)
            return one_shot_argv(cmd.shell, exe, cmd.script) if exe else None
        return list(cmd.argv or [])

    def _next_id(self) -> str:
        return f"j{len(self.jobs) + 1}"

    def _register(self, job_id: str, proc: asyncio.subprocess.Process) -> Job:
        job = Job(job_id, proc, self._log_path(job_id), time.time())
        job.log_path.parent.mkdir(parents=True, exist_ok=True)
        self.jobs[job_id] = job
        return job

    def _log_path(self, job_id: str) -> Path:
        return self.root / ".forge" / "jobs" / f"{job_id}.log"

    def _job(self, job_id: str) -> Job:
        if job_id not in self.jobs:
            raise JobNotFoundError(job_id, sorted(self.jobs))
        return self.jobs[job_id]

    def _log_lines(self, job: Job) -> list[str]:
        if not job.log_path.is_file():
            return []
        return clean_output(job.log_path.read_bytes()).splitlines()

    def _job_result(self, job: Job, text: str, total: int) -> CommandResult:
        return CommandResult(
            exit_code=job.exit_code,
            stdout=text,
            stderr="",
            job_id=job.id,
            pid=job.proc.pid,
            elapsed_s=job.elapsed(),
            total_lines=total,
        )


async def stop_tree(proc: asyncio.subprocess.Process) -> str:
    """Stop a process and its children: politely first, forcefully after 5 s."""
    if proc.returncode is not None:
        return "already exited"
    if sys.platform == "win32":
        await run_argv(["taskkill", "/PID", str(proc.pid), "/T"], Path.cwd())
        if await _exited_within(proc, STOP_GRACE_S):
            return "taskkill"
        await run_argv(["taskkill", "/PID", str(proc.pid), "/T", "/F"], Path.cwd())
        await _exited_within(proc, STOP_GRACE_S)
        return "taskkill /F"
    _signal_group(proc.pid, signal.SIGTERM)
    if await _exited_within(proc, STOP_GRACE_S):
        return "SIGTERM"
    _signal_group(proc.pid, signal.SIGKILL)
    await _exited_within(proc, STOP_GRACE_S)
    return "SIGKILL"


def _signal_group(pid: int, sig: signal.Signals) -> None:
    if sys.platform != "win32":
        with contextlib.suppress(ProcessLookupError):
            os.killpg(pid, sig)  # the process leads its own group (new session)


async def _exited_within(proc: asyncio.subprocess.Process, seconds: float) -> bool:
    try:
        await asyncio.wait_for(proc.wait(), seconds)
    except TimeoutError:
        return False
    return True
