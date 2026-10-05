"""Teams: sub-agents with their own context, started by the lead (`main`) agent.

`AgentRegistry` keeps one `AgentInfo` per agent of a session. A sub-agent gets a child
`Ctx` (own agent id, role and read ledger; same session, store, bus, renderer, sandbox and
shared state) and returns only its final report to the parent.
"""

import asyncio
import re
import time
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Literal

from forge.agent import AgentResult, run_agent
from forge.config import forge_home
from forge.ctx import Ctx, CustomRole
from forge.providers.base import Message, Usage
from forge.runtime.errors import ToolError
from forge.runtime.ledger import ReadLedger
from forge.tools import REGISTRY, TOOL_GROUPS

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
    started: float = field(default_factory=time.time)


class AgentRegistry:
    """All agents of one session; `main` is the lead."""

    def __init__(self) -> None:
        self.agents: dict[str, AgentInfo] = {}
        self._count = 0

    def find(self, key: str) -> AgentInfo | None:
        """An agent by id or name."""
        return self.agents.get(key) or next(
            (a for a in self.agents.values() if a.name == key), None
        )

    def running(self) -> int:
        """How many sub-agents are running now."""
        return sum(1 for a in self.agents.values() if a.status == "running")

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
        """Start a sub-agent for `task` and return its report (foreground)."""
        await use_agent_file(ctx, role)
        self.check_spawn(ctx, role, task, max_turns, name)
        if background or isolation != "none":
            raise ToolError("unsupported", "background and worktree agents are not available yet")
        info = self.add(role, task, name, ctx.agent_id)
        child = child_ctx(ctx, info)
        try:
            result = await run_agent(child, task, role=role, max_turns=max_turns)
        except asyncio.CancelledError:
            info.status = "cancelled"
            raise
        self.finish(info, result)
        await ctx.hooks.run("subagent_stop", {"agent_id": info.id, "status": info.status}, ctx)
        return report(info, result)

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


# ----------------------------------------------------------------------------- agent files

AGENT_KEYS = frozenset({"name", "model", "tools", "description"})


class AgentFileError(ValueError):
    """An agent file that cannot be used; the message names the file."""


@dataclass
class AgentDef:
    """A custom role from `.forge/agents/<name>.md` or `~/.forge/agents/<name>.md`."""

    name: str
    models: list[str]  # fallback chain; empty = the role's configured or default models
    tools: list[str] | None  # tool or group names; None = the coder's tools
    description: str
    prompt: str
    path: Path


def agent_dirs(root: Path) -> list[Path]:
    """User folder first, project folder last (so project files win on a name clash)."""
    return [forge_home() / "agents", root / ".forge" / "agents"]


def load_agents(root: Path) -> dict[str, AgentDef]:
    """Every agent file of the user and the project, by name."""
    agents: dict[str, AgentDef] = {}
    for folder in agent_dirs(root):
        for path in sorted(folder.glob("*.md")) if folder.is_dir() else []:
            agent = parse_agent_file(path)
            agents[agent.name] = agent
    return agents


def parse_agent_file(path: Path) -> AgentDef:
    """Front matter between `---` lines (name, model, tools, description), then the prompt."""
    text = path.read_text(encoding="utf-8").replace("\r\n", "\n")
    if not text.startswith("---\n") or "\n---" not in text[3:]:
        raise AgentFileError(f"{path}: must start with front matter between '---' lines")
    head, _, body = text[4:].partition("\n---")
    fields = parse_front_matter(head, path)
    name = fields.get("name") or path.stem
    if not isinstance(name, str) or not NAME.match(name):
        raise AgentFileError(f"{path}: name must match [a-z0-9-]{{1,32}}, got {name!r}")
    prompt = body.partition("\n")[2].strip()
    if not prompt:
        raise AgentFileError(f"{path}: the prompt (text after the front matter) is empty")
    return AgentDef(
        name=name,
        models=as_list(fields.get("model")),
        tools=as_list(fields["tools"]) if "tools" in fields else None,
        description=str(fields.get("description") or ""),
        prompt=prompt,
        path=path,
    )


def parse_front_matter(head: str, path: Path) -> dict[str, str | list[str]]:
    """`key: value` lines; lists as `[a, b]`, `a, b` or following `- item` lines."""
    fields: dict[str, str | list[str]] = {}
    key = ""
    for number, line in enumerate(head.splitlines(), start=2):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if line.lstrip().startswith("- ") and key:
            current = fields.get(key)
            fields[key] = [
                *(current if isinstance(current, list) else []),
                line.strip()[2:].strip(),
            ]
            continue
        key, colon, value = line.partition(":")
        key = key.strip()
        if not colon or key not in AGENT_KEYS:
            raise AgentFileError(
                f"{path}: line {number}: expected 'key: value' with key "
                f"{', '.join(sorted(AGENT_KEYS))}"
            )
        fields[key] = value.strip().strip("[]").strip()
    return fields


def as_list(value: str | list[str] | None) -> list[str]:
    """A comma-separated value or list, without blanks or quotes."""
    items = value if isinstance(value, list) else (value or "").split(",")
    return [i.strip().strip("'\"") for i in items if i.strip()]


async def use_agent_file(ctx: Ctx, role: str) -> None:
    """Install the agent file named `role` (if any) into the session: models, tools, prompt."""
    try:
        agents = await asyncio.to_thread(load_agents, ctx.root)
    except (AgentFileError, OSError, UnicodeDecodeError) as err:
        raise ToolError("invalid_args", f"bad agent file: {err}") from err
    agent = agents.get(role)
    if agent is None:
        return
    unknown = [t for t in agent.tools or [] if t not in REGISTRY and t not in TOOL_GROUPS]
    if unknown:
        raise ToolError("invalid_args", f"{agent.path}: unknown tools {', '.join(unknown)}")
    if agent.models:
        ctx.cfg.roles[agent.name] = agent.models
    ctx.state.custom_roles[agent.name] = CustomRole(agent.prompt, agent.tools, agent.description)
