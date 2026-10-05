"""Ctx: everything a tool or an agent may touch, passed explicitly."""

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from forge.config import ForgeConfig
from forge.hooks import Hooks
from forge.ports import EventBus, Executor, Renderer, Session, Store
from forge.runtime.permissions import Permissions


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
    ledger: Any  # ReadLedger once runtime/ledger.py exists (S07)
    permissions: Permissions
    hooks: Hooks
    agent_id: str = "main"
    role: str = "coder"
    headless: bool = False
