"""Ctx: everything a tool or an agent may touch, passed explicitly."""

from dataclasses import dataclass, field
from pathlib import Path

from forge.config import ForgeConfig
from forge.hooks import Hooks
from forge.ports import EventBus, Executor, Renderer, Session, Store
from forge.providers.base import Usage
from forge.runtime.ledger import ReadLedger
from forge.runtime.permissions import Permissions


@dataclass
class SessionState:
    """Runtime state shared by every agent of one session (never persisted)."""

    usage: Usage = field(default_factory=Usage)  # all model calls of the session so far
    notes: list[str] = field(default_factory=list)  # assumptions made before a spec exists
    failure: str = ""  # why the last step failed, for the replanner
    plan_rejected: bool = False  # the user rejected the plan without saying what to change
    checkpoints: dict[str, str] = field(default_factory=dict)  # step id -> snapshot ref (S18)
    web_searches: int = 0  # web_search calls so far (limit 200 per session)


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
