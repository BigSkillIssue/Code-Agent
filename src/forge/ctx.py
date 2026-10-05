"""Ctx: everything a tool or an agent may touch, passed explicitly."""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from forge.config import ForgeConfig
from forge.hooks import Hooks
from forge.ports import EventBus, Executor, Renderer, Session, Store
from forge.providers.base import Usage
from forge.runtime.ledger import ReadLedger
from forge.runtime.permissions import Permissions


@dataclass
class CustomRole:
    """A role defined by an agent file (.forge/agents/<name>.md)."""

    prompt: str  # the file's body, added to the team-member prompt
    tools: list[str] | None = None  # tool or group names; None = the coder's tools
    description: str = ""


class Team(Protocol):
    """The session's agents (implemented by team.AgentRegistry; tools reach it through Ctx)."""

    async def spawn(
        self,
        ctx: "Ctx",
        role: str,
        task: str,
        *,
        background: bool,
        isolation: str,
        max_turns: int,
        name: str | None,
    ) -> str: ...
    async def send(self, ctx: "Ctx", to: str, text: str, summary: str) -> str: ...
    def overview(self, ctx: "Ctx") -> str: ...
    async def stop(self, ctx: "Ctx", agent: str, keep_worktree: bool) -> str: ...
    def take_messages(self, agent_id: str) -> list[str]: ...
    async def wait_for_message(self, agent_id: str) -> list[str]: ...
    def has_running_children(self, agent_id: str) -> bool: ...
    def note_job(self, agent_id: str, job_id: str) -> None: ...
    def record_turn(self, agent_id: str, usage: Usage) -> None: ...


@dataclass
class SessionState:
    """Runtime state shared by every agent of one session (never persisted)."""

    usage: Usage = field(default_factory=Usage)  # all model calls of the session so far
    notes: list[str] = field(default_factory=list)  # assumptions made before a spec exists
    failure: str = ""  # why the last step failed, for the replanner
    plan_rejected: bool = False  # the user rejected the plan without saying what to change
    checkpoints: dict[str, str] = field(default_factory=dict)  # step id -> snapshot ref (S18)
    web_searches: int = 0  # web_search calls so far (limit 200 per session)
    compact_request: int = 0  # /compact asks for level 2 (or 3 with "hard") on the next turn
    context_usage: dict[str, int] = field(default_factory=dict)  # last request, by category
    team: Team | None = None  # set by wiring; None means sub-agents are unavailable
    custom_roles: dict[str, CustomRole] = field(default_factory=dict)  # from agent files


@dataclass
class Ctx:
    """The working context of one agent in one session."""

    session: Session
    cfg: ForgeConfig
    root: Path  # project root, resolved
    cwd: Path  # current dir, always inside root
    store: Store
    bus: EventBus
    executor: Executor
    renderer: Renderer
    ledger: ReadLedger  # path -> sha256 of last read content
    permissions: Permissions
    hooks: Hooks
    agent_id: str = "main"
    role: str = "coder"
    headless: bool = False
    state: SessionState = field(default_factory=SessionState)  # beyond the contract, see PROGRESS
