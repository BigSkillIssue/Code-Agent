"""Teams: sub-agents with their own context, started by the lead (`main`) agent.

`AgentRegistry` keeps one `AgentInfo` per agent of a session. A sub-agent gets a child
`Ctx` (own agent id, role and read ledger; same session, store, bus, renderer, sandbox and
shared state) and returns only its final report to the parent.
"""

import asyncio
import re
import time
from dataclasses import dataclass, field, replace
from typing import Literal

from forge import board
from forge.agent import AgentResult, run_agent
from forge.agent_files import use_agent_file
from forge.ctx import Ctx
from forge.events import AgentFinished, AgentMessage
from forge.ports import JobNotFoundError
from forge.providers.base import Message, Usage
from forge.runtime.errors import ToolError
from forge.runtime.ledger import ReadLedger

BUILTIN_ROLES = ("explore", "coder", "tester", "reviewer", "researcher")
NAME = re.compile(r"^[a-z0-9-]{1,32}$")
AgentStatus = Literal["running", "done", "stopped", "failed", "cancelled"]
STATUS_OF: dict[str, AgentStatus] = {
    "done": "done",
    "max_turns": "stopped",
    "budget": "stopped",
    "error": "failed",
    "cancelled": "cancelled",
}


@dataclass
class AgentInfo:
    """One agent of the session."""

    id: str
    name: str | None
    role: str
    status: AgentStatus
    parent_id: str | None
    task: str
    turns: int = 0
    usage: Usage = field(default_factory=Usage)
    worktree: str | None = None
    inbox: "asyncio.Queue[str]" = field(default_factory=asyncio.Queue)
    task_handle: "asyncio.Task[None] | None" = None
    messages: list[Message] = field(default_factory=list)  # the agent's own transcript
    jobs: list[str] = field(default_factory=list)  # background jobs it started
    started: float = field(default_factory=time.time)


class AgentRegistry:
    """All agents of one session; `main` is the lead."""

    def __init__(self) -> None:
        self.agents: dict[str, AgentInfo] = {
            "main": AgentInfo("main", None, "coder", "running", None, "")
        }
        self._count = 0

    def find(self, key: str) -> AgentInfo | None:
        """An agent by id or name."""
        return self.agents.get(key) or next(
            (a for a in self.agents.values() if a.name == key), None
        )

    def running(self) -> int:
        """How many sub-agents are running now."""
        return sum(1 for a in self.agents.values() if a.status == "running" and a.id != "main")

    def add(self, role: str, task: str, name: str | None, parent_id: str) -> AgentInfo:
        """Register a new running agent with the next id (a1, a2, ...)."""
        self._count += 1
        info = AgentInfo(f"a{self._count}", name, role, "running", parent_id, task)
        self.agents[info.id] = info
        return info

    async def spawn(
        self,
        ctx: Ctx,
        role: str,
        task: str,
        *,
        background: bool,
        isolation: str,
        max_turns: int,
        name: str | None,
    ) -> str:
        """Start a sub-agent for `task`: wait for its report, or run it in the background."""
        await use_agent_file(ctx, role)
        self.check_spawn(ctx, role, task, max_turns, name)
        if isolation != "none":
            raise ToolError("unsupported", "worktree agents are not available yet")
        info = self.add(role, task, name, ctx.agent_id)
        child = child_ctx(ctx, info)
        if background:
            info.task_handle = asyncio.create_task(self.run_background(ctx, child, info, max_turns))
            started = f"started agent {info.id} ({role}) in background"
            return f"{started}; you will receive its report as a message"
        try:
            result = await run_agent(child, task, role=role, max_turns=max_turns)
        except asyncio.CancelledError:
            info.status = "cancelled"
            raise
        self.finish(info, result)
        await ctx.hooks.run("subagent_stop", {"agent_id": info.id, "status": info.status}, ctx)
        return report(info, result)

    async def run_background(self, ctx: Ctx, child: Ctx, info: AgentInfo, max_turns: int) -> None:
        """Run a background agent, then post its report to the parent's inbox."""
        try:
            result = await run_agent(child, info.task, role=info.role, max_turns=max_turns)
        except asyncio.CancelledError:
            info.status = "cancelled"
            return
        except Exception as exc:  # a crashing sub-agent must not take the lead down with it
            info.status, text = "failed", f"{type(exc).__name__}: {exc}"
        else:
            self.finish(info, result)
            text = result.text.strip()
        head = f"[agent {info.id} ({info.role}) finished: {info.status}]"
        self.post(info.parent_id or "main", f"{head}\n{text or '(no report)'}")
        await ctx.bus.publish(
            AgentFinished(
                session_id=ctx.session.id,
                agent_id=info.id,
                ts=time.time(),
                role=info.role,
                status=info.status,
                report=text,
            )
        )
        await ctx.hooks.run("subagent_stop", {"agent_id": info.id, "status": info.status}, ctx)

    def check_spawn(self, ctx: Ctx, role: str, task: str, max_turns: int, name: str | None) -> None:
        """The spawn rules: lead only, known role, valid inputs, limits."""
        if ctx.agent_id != "main":
            raise ToolError("unsupported", "only the lead agent can start sub-agents")
        if role not in BUILTIN_ROLES and role not in ctx.state.custom_roles:
            known = ", ".join([*BUILTIN_ROLES, *ctx.state.custom_roles])
            raise ToolError("not_found", f"unknown role '{role}'", hint=f"roles: {known}")
        if not 1 <= len(task) <= 20_000 or not task.strip():
            raise ToolError("invalid_args", "task must be 1-20,000 characters")
        if not 1 <= max_turns <= 200:
            raise ToolError("invalid_args", "max_turns must be between 1 and 200")
        if name is not None and (not NAME.match(name) or self.find(name) is not None):
            raise ToolError(
                "invalid_args", f"name '{name}' must match [a-z0-9-]{{1,32}} and be unique"
            )
        limits = ctx.cfg.limits
        if self.running() >= limits.max_parallel_agents:
            raise ToolError(
                "limit_reached", f"{limits.max_parallel_agents} agents are already running"
            )
        if ctx.state.usage.cost_usd >= limits.max_cost_usd:
            raise ToolError(
                "limit_reached", f"the session budget of ${limits.max_cost_usd:.2f} is used up"
            )

    def finish(self, info: AgentInfo, result: AgentResult) -> None:
        """Record how an agent ended."""
        info.status = STATUS_OF[result.stopped]
        info.usage = result.usage
        info.messages = result.messages
        info.turns = sum(1 for m in result.messages if m.role == "assistant")

    # ------------------------------------------------------------------ messages

    def post(self, agent_id: str, text: str) -> None:
        """Put a message into an agent's inbox."""
        self.agents[agent_id].inbox.put_nowait(text)

    async def send(self, ctx: Ctx, to: str, text: str, summary: str) -> str:
        """send_message: deliver `text` to a running agent's inbox."""
        if not text.strip() or len(text) > 10_000 or len(summary) > 100:
            raise ToolError(
                "invalid_args", "text must be 1-10,000 and summary at most 100 characters"
            )
        target = self.find(to)
        if target is None:
            raise ToolError(
                "not_found", f"no agent '{to}'", hint="agents: " + ", ".join(self.agents)
            )
        if target.id == ctx.agent_id:
            raise ToolError("invalid_args", "you cannot send a message to yourself")
        if target.status != "running":
            raise ToolError(
                "not_found",
                f"agent {target.id} is not running",
                hint=f"agent {target.id} has finished",
            )
        self.post(target.id, f"[message from {ctx.agent_id} ({ctx.role})]: {text}")
        await ctx.bus.publish(
            AgentMessage(
                session_id=ctx.session.id,
                agent_id=ctx.agent_id,
                ts=time.time(),
                to=target.id,
                summary=summary or text[:100],
                text=text,
            )
        )
        return f"delivered to {target.id} ({target.role})"

    def take_messages(self, agent_id: str) -> list[str]:
        """Every message waiting in an agent's inbox."""
        info = self.agents.get(agent_id)
        texts: list[str] = []
        while info is not None and not info.inbox.empty():
            texts.append(info.inbox.get_nowait())
        return texts

    async def wait_for_message(self, agent_id: str) -> list[str]:
        """Wait for the next message, then return it with any others already waiting."""
        first = await self.agents[agent_id].inbox.get()
        return [first, *self.take_messages(agent_id)]

    def has_running_children(self, agent_id: str) -> bool:
        """True while a sub-agent this agent started is still running."""
        return any(a.parent_id == agent_id and a.status == "running" for a in self.agents.values())

    def record_turn(self, agent_id: str, usage: Usage) -> None:
        """Count one model turn of an agent."""
        info = self.agents.get(agent_id)
        if info is not None:
            info.turns += 1
            info.usage += usage

    def note_job(self, agent_id: str, job_id: str) -> None:
        """Remember a background job, so stopping the agent stops the job too."""
        info = self.agents.get(agent_id)
        if info is not None:
            info.jobs.append(job_id)

    # ------------------------------------------------------------------ list and stop

    async def read_board(self, ctx: Ctx, statuses: list[str] | None) -> str:
        """read_board: the plan as tasks with owners."""
        return await board.read_board(ctx, statuses)

    async def claim_task(self, ctx: Ctx, task_id: str) -> str:
        """claim_task: take a ready task."""
        return await board.claim(ctx, task_id)

    async def update_task(self, ctx: Ctx, task_id: str, status: str, result: str) -> str:
        """update_task: report progress or the result; the lead is told."""
        lead = "main"

        def notify(text: str) -> None:
            if ctx.agent_id != lead:
                self.post(lead, text)

        return await board.update(ctx, task_id, status, result, notify)

    def overview(self, ctx: Ctx) -> str:
        """list_agents: one row per agent."""
        main = self.agents["main"]
        if not main.task:
            main.task = ctx.session.spec.goal if ctx.session.spec else first_user_text(ctx)
        rows = [f"{'id':<6}{'name':<11}{'role':<10}{'status':<10}{'turns':<7}{'tokens':<8}task"]
        for info in self.agents.values():
            tokens = f"{(info.usage.input_tokens + info.usage.output_tokens) / 1000:.1f}k"
            rows.append(
                f"{info.id:<6}{info.name or '-':<11}{info.role:<10}{info.status:<10}"
                f"{info.turns:<7}{tokens:<8}{' '.join(info.task.split())[:50]}"
            )
        return "\n".join(rows)

    async def stop(self, ctx: Ctx, agent: str, keep_worktree: bool) -> str:
        """stop_agent: cancel a running agent and stop its background jobs."""
        if ctx.agent_id != "main":
            raise ToolError("unsupported", "only the lead agent can stop agents")
        info = self.find(agent)
        if info is None or info.id == "main":
            raise ToolError("not_found", f"no sub-agent '{agent}'")
        if info.status != "running":
            raise ToolError(
                "invalid_args",
                f"{info.id} is not running",
                hint=f"it ended with status {info.status}",
            )
        if info.task_handle is not None:
            info.task_handle.cancel()
            await asyncio.wait({info.task_handle}, timeout=1.0)
        info.status = "cancelled"
        stopped = [job for job in info.jobs if await stop_job(ctx, job)]
        released = await board.release_owned(ctx, info.id)
        text = f"stopped {info.id} ({info.role}) after {info.turns} turns"
        if stopped:
            text += f"; stopped jobs {', '.join(stopped)}"
        if released:
            text += f"; released task {', '.join(released)}"
        return text


async def stop_job(ctx: Ctx, job_id: str) -> bool:
    """Stop one background job; False if it no longer exists."""
    try:
        await ctx.executor.job_stop(job_id)
    except JobNotFoundError:
        return False
    return True


def first_user_text(ctx: Ctx) -> str:
    """The first user message of the session (the task, before a spec exists)."""
    return next((m.text() for m in ctx.session.messages if m.role == "user"), "")


def child_ctx(ctx: Ctx, info: AgentInfo) -> Ctx:
    """The sub-agent's context: own id, role and read ledger; everything else shared."""
    return replace(ctx, agent_id=info.id, role=info.role, ledger=ReadLedger())


def report(info: AgentInfo, result: AgentResult) -> str:
    """What the parent sees: one status line and the sub-agent's final text."""
    label = f"agent {info.id} ({info.role})"
    if info.status == "done":
        tokens = (info.usage.input_tokens + info.usage.output_tokens) / 1000
        cost = info.usage.cost_usd
        head = f"{label} finished: done, {info.turns} turns, {tokens:.1f}k tokens, ${cost:.2f}"
    elif info.status == "stopped":
        head = f"{label} stopped: {result.stopped} (partial report)"
    else:
        head = (
            f"{label} {info.status}: {result.text[:200] if info.status == 'failed' else ''}".rstrip(
                ": "
            )
        )
    return f"{head}\n--- report ---\n{result.text.strip() or '(no report)'}"
