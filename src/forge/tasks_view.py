"""Everything running in the background, in one list: shell jobs, sub-agents, monitors (S55)."""

import time
from dataclasses import dataclass
from typing import Any, Literal

from forge.ctx import Ctx
from forge.ports import JobNotFoundError
from forge.runtime.errors import ToolError

DETAIL_LINES = 40


@dataclass
class TaskRow:
    """One background task as the user sees it."""

    id: str  # j3, a1, m2
    kind: Literal["job", "agent", "monitor"]
    description: str
    status: str  # running, done, exit 0, stopped, ...
    seconds: float
    last_line: str = ""


async def list_tasks(ctx: Ctx) -> list[TaskRow]:
    """Jobs (except the ones monitors watch), monitors and sub-agents, oldest first."""
    monitors = list(ctx.state.monitors.all.values())
    watched = {m.job_id for m in monitors}
    rows = []
    for job in jobs_of(ctx).values():
        if job.id in watched:
            continue
        status = "running" if job.ended is None else f"exit {job.exit_code}"
        label = ctx.state.job_labels.get(job.id, f"job {job.id}")
        rows.append(
            TaskRow(job.id, "job", label, status, job.elapsed(), await last_line(ctx, job.id))
        )
    for m in monitors:
        seconds = time.monotonic() - m.started
        rows.append(TaskRow(m.id, "monitor", m.description, m.status, seconds))
    for info in agents_of(ctx).values():
        if info.id == "main":
            continue
        description = f"{info.role}: {info.task.strip().splitlines()[0][:60]}"
        rows.append(TaskRow(info.id, "agent", description, info.status, time.time() - info.started))
    return rows


def running_count(rows: list[TaskRow]) -> int:
    """How many rows are still running."""
    return sum(row.status == "running" for row in rows)


async def tasks_report(ctx: Ctx) -> str:
    """`/tasks`: one line per task."""
    rows = await list_tasks(ctx)
    if not rows:
        return "no background tasks"
    lines = [row_text(row) for row in rows]
    return "\n".join([*lines, "/tasks <id> shows a task; /tasks stop <id> stops it"])


def row_text(row: TaskRow) -> str:
    """One aligned line."""
    last = f"  | {row.last_line[:60]}" if row.last_line else ""
    return (
        f"{row.id:<4} {row.kind:<8} {row.status:<10} {row.seconds:>5.0f}s  {row.description}{last}"
    )


async def task_detail(ctx: Ctx, task_id: str) -> str:
    """The output of a job or monitor, or an agent's status and latest words."""
    monitor = ctx.state.monitors.all.get(task_id)
    if monitor is not None:
        return f"monitor {task_id}: {monitor.status}\n" + await tail(ctx, monitor.job_id)
    if task_id in jobs_of(ctx):
        return await tail(ctx, task_id)
    info = agents_of(ctx).get(task_id)
    if info is not None and task_id != "main":
        said = next((m.text() for m in reversed(info.messages) if m.role == "assistant"), "")
        return f"agent {task_id} ({info.role}): {info.status}, {info.turns} turns\n{said}".rstrip()
    return f"no task {task_id}"


async def stop_task(ctx: Ctx, task_id: str) -> str:
    """Stop a job, monitor or agent."""
    try:
        if task_id in ctx.state.monitors.all:
            await ctx.state.monitors.stop(ctx, task_id)
        elif task_id in jobs_of(ctx):
            await ctx.executor.job_stop(task_id)
        elif task_id in agents_of(ctx) and task_id != "main" and ctx.state.team is not None:
            await ctx.state.team.stop(ctx, task_id, keep_worktree=True)
        else:
            return f"no task {task_id}"
    except (ToolError, JobNotFoundError) as exc:
        return f"could not stop {task_id}: {exc}"
    return f"stopped {task_id}"


async def tail(ctx: Ctx, job_id: str) -> str:
    """The last lines of a job's log."""
    total = await total_lines(ctx, job_id)
    try:
        result = await ctx.executor.job_output(job_id, since_line=max(0, total - DETAIL_LINES))
    except JobNotFoundError:
        return "(the job is gone)"
    return result.stdout or "(no output yet)"


async def last_line(ctx: Ctx, job_id: str) -> str:
    """The newest line of a job's log."""
    lines = (await tail(ctx, job_id)).splitlines()
    return lines[-1] if lines and not lines[-1].startswith("(") else ""


async def total_lines(ctx: Ctx, job_id: str) -> int:
    """Lines in a job's log so far."""
    try:
        result = await ctx.executor.job_output(job_id, since_line=10**9)
    except JobNotFoundError:
        return 0
    return result.total_lines or 0


def jobs_of(ctx: Ctx) -> dict[str, Any]:
    """The executor's jobs (the local executor keeps them; other executors may not)."""
    jobs: dict[str, Any] = getattr(ctx.executor, "jobs", {})
    return jobs


def agents_of(ctx: Ctx) -> dict[str, Any]:
    """The session's agents, if the team keeps a registry."""
    agents: dict[str, Any] = getattr(ctx.state.team, "agents", {})
    return agents
