"""Test helpers shared by many test modules (fixtures live in conftest.py)."""

import asyncio
import subprocess
from collections.abc import Callable
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
from forge.team import AgentRegistry


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
        self,
        cmd: Command,
        policy: SandboxPolicy,
        background: bool = False,
        on_output: Callable[[str], None] | None = None,
    ) -> CommandResult:
        raise AssertionError(f"unexpected command: {cmd}")

    async def job_output(self, job_id: str, since_line: int = 0) -> CommandResult:
        raise AssertionError("unexpected job_output")

    async def job_stop(self, job_id: str) -> CommandResult:
        raise AssertionError("unexpected job_stop")


class ScriptedExecutor:
    """An Executor that runs nothing: `answer(cmd)` decides each command's result (None: exit 0),
    background jobs print `job_lines[program]` and run until stopped (or `ended` names them)."""

    def __init__(
        self,
        answer: Callable[[Command], CommandResult | None] | None = None,
        job_lines: dict[str, list[str]] | None = None,
    ) -> None:
        self.answer = answer or (lambda cmd: None)
        self.job_lines = job_lines or {}
        self.commands: list[Command] = []
        self.jobs: dict[str, list[str]] = {}
        self.ended: set[str] = set()
        self.stopped: list[str] = []

    @staticmethod
    def words(cmd: Command) -> list[str]:
        """The command's argv with the program as a bare name (`/usr/bin/npm` -> `npm`)."""
        argv = cmd.argv or []
        if not argv:
            return []
        name = Path(argv[0]).name
        for suffix in (".exe", ".cmd", ".bat"):
            name = name.removesuffix(suffix)
        return [name, *argv[1:]]

    async def run(
        self,
        cmd: Command,
        policy: SandboxPolicy,
        background: bool = False,
        on_output: Callable[[str], None] | None = None,
    ) -> CommandResult:
        self.commands.append(cmd)
        if background:
            job_id = f"job{len(self.jobs) + 1}"
            self.jobs[job_id] = list(self.job_lines.get(self.words(cmd)[0], []))
            return CommandResult(exit_code=None, stdout="", stderr="", job_id=job_id)
        return self.answer(cmd) or CommandResult(exit_code=0, stdout="", stderr="")

    async def job_output(self, job_id: str, since_line: int = 0) -> CommandResult:
        lines = self.jobs[job_id]
        code = 1 if job_id in self.ended else None
        text = "\n".join(lines[since_line:])
        return CommandResult(exit_code=code, stdout=text, stderr="", total_lines=len(lines))

    async def job_stop(self, job_id: str) -> CommandResult:
        self.stopped.append(job_id)
        return CommandResult(exit_code=-15, stdout="", stderr="SIGTERM")

    def ran(self, *words: str) -> list[Command]:
        """The commands that started with these words."""
        return [c for c in self.commands if self.words(c)[: len(words)] == list(words)]


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
    ctx = Ctx(
        session=session,
        cfg=cfg,
        root=root.resolve(),
        cwd=root.resolve(),
        store=fields.pop("store", None) or MemoryStore(),
        bus=MemoryBus(),
        executor=executor or NoExecutor(),
        renderer=renderer or ScriptedRenderer(),
        ledger=ReadLedger(),
        permissions=Permissions(cfg),
        hooks=Hooks(cfg),
        headless=headless,
        **fields,
    )
    ctx.state.team = AgentRegistry()
    return ctx


async def drain(subscription: Any, wait_s: float = 0.05) -> list[Event]:
    """Every event already queued on a bus subscription (waits briefly for stragglers)."""
    events: list[Event] = []
    while True:
        try:
            events.append(await asyncio.wait_for(anext(subscription), wait_s))
        except TimeoutError:
            return events
