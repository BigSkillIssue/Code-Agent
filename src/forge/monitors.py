"""Monitors: watch a background command and send its new output lines to an agent (S53).

A monitor polls its job's log through the Executor port and posts new lines (optionally
filtered) to the agent's inbox; the agent loop waits for such messages while monitors run.
"""

import asyncio
import contextlib
import re
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal

from forge.ports import JobNotFoundError
from forge.runtime.errors import ToolError

if TYPE_CHECKING:
    from forge.ctx import Ctx

POLL_S = 0.5
MAX_LINES = 50  # per message; the rest is counted, the log keeps everything
Status = Literal["running", "ended", "stopped", "timed_out"]


@dataclass
class Monitor:
    """One watched background job."""

    id: str
    agent_id: str
    job_id: str
    description: str
    pattern: re.Pattern[str] | None
    timeout_s: float
    started: float = field(default_factory=time.monotonic)
    status: Status = "running"
    line: int = 0  # log lines already read
    sent: int = 0  # lines sent to the agent
    task: "asyncio.Task[None] | None" = None


class Monitors:
    """Every monitor of the session."""

    def __init__(self) -> None:
        self.all: dict[str, Monitor] = {}

    def active(self, agent_id: str) -> bool:
        """True while one of the agent's monitors still runs."""
        return any(m.agent_id == agent_id and m.status == "running" for m in self.all.values())

    def start(
        self, ctx: "Ctx", job_id: str, description: str, pattern: str, timeout_s: float
    ) -> Monitor:
        """Watch `job_id` for the calling agent."""
        monitor = Monitor(
            id=f"m{len(self.all) + 1}",
            agent_id=ctx.agent_id,
            job_id=job_id,
            description=description,
            pattern=re.compile(pattern) if pattern else None,
            timeout_s=timeout_s,
        )
        self.all[monitor.id] = monitor
        monitor.task = asyncio.get_running_loop().create_task(watch(ctx, monitor))
        return monitor

    async def stop(self, ctx: "Ctx", monitor_id: str) -> Monitor:
        """Stop a running monitor and its command."""
        monitor = self.all.get(monitor_id)
        if monitor is None or monitor.status != "running":
            state = "unknown" if monitor is None else monitor.status
            raise ToolError("invalid_args", f"monitor {monitor_id} is not running ({state})")
        monitor.status = "stopped"
        if monitor.task is not None:
            monitor.task.cancel()
        await stop_job(ctx, monitor.job_id)
        return monitor

    async def stop_agent(self, ctx: "Ctx", agent_id: str) -> None:
        """Stop every monitor of an agent that has finished."""
        for monitor in list(self.all.values()):
            if monitor.agent_id == agent_id and monitor.status == "running":
                await self.stop(ctx, monitor.id)

    async def close(self) -> None:
        """At session end: stop watching (the executor stops the jobs)."""
        for monitor in self.all.values():
            if monitor.task is not None:
                monitor.task.cancel()
            if monitor.status == "running":
                monitor.status = "stopped"


async def watch(ctx: "Ctx", monitor: Monitor) -> None:
    """Poll the job's log; post new lines; post a last message when the job ends."""
    while monitor.status == "running":
        await asyncio.sleep(POLL_S)
        try:
            result = await ctx.executor.job_output(monitor.job_id, since_line=monitor.line)
        except JobNotFoundError:
            result = None
        if result is None:
            finish(ctx, monitor, "ended", "its job is gone", [])
            return
        lines = result.stdout.splitlines() if result.stdout else []
        monitor.line = result.total_lines if result.total_lines is not None else monitor.line
        wanted = [line for line in lines if monitor.pattern is None or monitor.pattern.search(line)]
        if result.exit_code is not None:
            finish(ctx, monitor, "ended", f"exit code {result.exit_code}", wanted)
            return
        if time.monotonic() - monitor.started > monitor.timeout_s:
            await stop_job(ctx, monitor.job_id)
            finish(ctx, monitor, "timed_out", "", wanted)
            return
        if wanted:
            post(ctx, monitor, f"[monitor {monitor.id}: {monitor.description}]", wanted)


def finish(ctx: "Ctx", monitor: Monitor, status: Status, why: str, lines: list[str]) -> None:
    """Mark the monitor done (before posting, so a woken agent sees it as done)."""
    monitor.status = status
    if status == "timed_out":
        head = f"[monitor {monitor.id} timed out after {monitor.timeout_s:.0f}s; it was stopped"
    else:
        head = f"[monitor {monitor.id} ended: {why}"
    post(ctx, monitor, f"{head} ({monitor.description})]", lines)


def post(ctx: "Ctx", monitor: Monitor, head: str, lines: list[str]) -> None:
    """Send lines to the agent's inbox, capped per message."""
    team = ctx.state.team
    if team is None:
        return
    shown = lines[:MAX_LINES]
    monitor.sent += len(shown)
    more = [f"... {len(lines) - len(shown)} more lines in .forge/jobs/{monitor.job_id}.log"]
    team.post(
        monitor.agent_id, "\n".join([head, *shown, *(more if len(lines) > len(shown) else [])])
    )


async def stop_job(ctx: "Ctx", job_id: str) -> None:
    """Stop a job that may already have ended."""
    with contextlib.suppress(JobNotFoundError):
        await ctx.executor.job_stop(job_id)
