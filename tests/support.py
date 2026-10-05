"""Test helpers shared by many test modules (fixtures live in conftest.py)."""

import subprocess
from pathlib import Path
from typing import Any

from forge.config import ForgeConfig
from forge.ctx import Ctx
from forge.events import Event
from forge.hooks import Hooks
from forge.local.memory_bus import MemoryBus
from forge.local.memory_store import MemoryStore
from forge.plan import Question
from forge.ports import (
    Answer,
    Approval,
    Command,
    CommandResult,
    Executor,
    SandboxPolicy,
    Session,
)
from forge.providers.base import ToolCall
from forge.runtime.ledger import ReadLedger
from forge.runtime.permissions import Permissions


class ScriptedRenderer:
    """A Renderer that records events and answers from prepared lists."""

    def __init__(
        self, approvals: list[Approval] | None = None, answers: list[list[Answer]] | None = None
    ) -> None:
        self.events: list[Event] = []
        self.approvals = list(approvals or [])
        self.answers = list(answers or [])
        self.approval_requests: list[tuple[ToolCall, str]] = []
        self.questions: list[list[Question]] = []

    async def show(self, event: Event) -> None:
        self.events.append(event)

    async def ask(self, questions: list[Question]) -> list[Answer]:
        self.questions.append(questions)
        if self.answers:
            return self.answers.pop(0)
        return [Answer(question_index=i, values=[q.default or ""]) for i, q in enumerate(questions)]

    async def approve(self, call: ToolCall, reason: str) -> Approval:
        self.approval_requests.append((call, reason))
        return self.approvals.pop(0) if self.approvals else Approval(allow=True)


class NoExecutor:
    """An Executor for tests that must not run commands."""

    async def run(
        self, cmd: Command, policy: SandboxPolicy, background: bool = False
    ) -> CommandResult:
        raise AssertionError(f"unexpected command: {cmd}")

    async def job_output(self, job_id: str, since_line: int = 0) -> CommandResult:
        raise AssertionError("unexpected job_output")

    async def job_stop(self, job_id: str) -> CommandResult:
        raise AssertionError("unexpected job_stop")


def git(root: Path, *args: str) -> str:
    """Run git in `root` and return its stdout."""
    proc = subprocess.run(["git", *args], cwd=root, capture_output=True, text=True, check=True)
    return proc.stdout


def init_repo(root: Path) -> Path:
    """Make `root` a git repository with a deterministic, unsigned identity."""
    root.mkdir(parents=True, exist_ok=True)
    git(root, "init", "-q", "-b", "main")
    git(root, "config", "user.email", "test@example.com")
    git(root, "config", "user.name", "Forge Test")
    git(root, "config", "commit.gpgsign", "false")
    return root


def make_ctx(
    root: Path,
    *,
    cfg: ForgeConfig | None = None,
    renderer: Any = None,
    executor: Executor | None = None,
    headless: bool = False,
    **fields: Any,
) -> Ctx:
    """A Ctx over in-memory ports for `root`."""
    cfg = cfg or ForgeConfig()
    session = Session(id="test-session", project_root=str(root), created_at=0.0, status="active")
    return Ctx(
        session=session,
        cfg=cfg,
        root=root.resolve(),
        cwd=root.resolve(),
        store=MemoryStore(),
        bus=MemoryBus(),
        executor=executor or NoExecutor(),
        renderer=renderer or ScriptedRenderer(),
        ledger=ReadLedger(),
        permissions=Permissions(cfg),
        hooks=Hooks(cfg),
        headless=headless,
        **fields,
    )
