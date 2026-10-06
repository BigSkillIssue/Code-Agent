"""The four seams where a server can plug in later: Store, EventBus, Executor, Renderer.

v1 implements them in `forge.local`; the core only ever sees these interfaces.
"""

from collections.abc import AsyncIterator
from typing import Literal, Protocol, runtime_checkable

from pydantic import BaseModel

from forge.events import Event
from forge.plan import Plan, Question, TaskSpec
from forge.providers.base import ImagePart, Message, ToolCall


class SessionNotFoundError(LookupError):
    """No session with that id exists in the store."""


class JobNotFoundError(LookupError):
    """No background job with that id exists; `known` lists the ids that do."""

    def __init__(self, job_id: str, known: list[str]) -> None:
        super().__init__(job_id)
        self.known = known


class Session(BaseModel):
    """One task from prompt to report; saved after every step so it can resume."""

    id: str  # uuid4 hex
    project_root: str
    created_at: float
    status: Literal["active", "waiting", "done", "failed", "cancelled"]
    spec: TaskSpec | None = None
    plan: Plan | None = None
    messages: list[Message] = []  # full transcript, never compacted
    summary: str = ""  # latest compaction summary


class Store(Protocol):
    """Persists sessions and searches earlier ones."""

    async def create_session(self, project_root: str) -> Session: ...
    async def save_session(self, s: Session) -> None: ...
    async def load_session(self, session_id: str) -> Session: ...
    async def list_sessions(self, project_root: str, limit: int = 20) -> list[Session]: ...
    async def search(self, project_root: str, query: str, limit: int = 5) -> list[tuple[str, str]]:
        """(session_id, excerpt) pairs for `recall`."""
        ...


@runtime_checkable
class BoardStore(Protocol):
    """Task owners for teams (beyond the contract): one owner per step, claimed atomically."""

    async def claim(self, session_id: str, step_id: str, owner: str) -> str:
        """Set the owner if the step has none; returns the owner after the call."""
        ...

    async def release(self, session_id: str, step_id: str) -> None:
        """Clear the step's owner."""
        ...

    async def owners(self, session_id: str) -> dict[str, str]:
        """step id -> owner for every owned step."""
        ...


class EventBus(Protocol):
    """Carries every event the core reports to whoever renders it."""

    async def publish(self, event: Event) -> None: ...
    def subscribe(self, session_id: str) -> AsyncIterator[Event]: ...


class Command(BaseModel):
    """A command to run: an argv list, or a script for a persistent shell."""

    argv: list[str] | None = None  # exec form, or
    script: str | None = None  # text sent to a persistent shell
    shell: Literal["bash", "powershell", "none"] = "none"
    cwd: str
    timeout_s: float = 120
    env: dict[str, str] = {}


class CommandResult(BaseModel):
    """What a command produced."""

    exit_code: int | None  # None = still running (background)
    stdout: str
    stderr: str
    timed_out: bool = False
    sandbox_denied: bool = False  # failed because the sandbox blocked it
    job_id: str | None = None
    # Beyond the contract (see PROGRESS.md, open issues): what the shell and job tools report.
    cwd: str | None = None  # working directory after a persistent-shell command
    pid: int | None = None  # process id of a background job
    elapsed_s: float | None = None  # how long a job has been running (or ran)
    total_lines: int | None = None  # lines in a job's log, for job_output paging


class SandboxPolicy(BaseModel):
    """What a command may touch."""

    mode: Literal["read-only", "workspace-write", "full-access"] = "workspace-write"
    writable_roots: list[str] = []
    network: bool = False


class Executor(Protocol):
    """Runs commands inside the sandbox and manages background jobs."""

    async def run(
        self, cmd: Command, policy: SandboxPolicy, background: bool = False
    ) -> CommandResult: ...
    async def job_output(self, job_id: str, since_line: int = 0) -> CommandResult: ...
    async def job_stop(self, job_id: str) -> CommandResult: ...


class Answer(BaseModel):
    """The user's answer to one question."""

    question_index: int
    values: list[str]  # chosen options or typed text


class Approval(BaseModel):
    """The user's decision on a tool call that needs approval."""

    allow: bool
    remember: bool = False  # add an allow rule for this session
    feedback: str = ""  # "no, do X instead" goes back to the model


class Renderer(Protocol):
    """Shows events, asks questions and asks for approvals."""

    async def show(self, event: Event) -> None: ...
    async def ask(self, questions: list[Question]) -> list[Answer]: ...
    async def approve(self, call: ToolCall, reason: str) -> Approval: ...


class BrowserError(Exception):
    """An expected browser failure (no browser installed, element not found, page timeout)."""

    def __init__(self, message: str, hint: str = "") -> None:
        super().__init__(message)
        self.hint = hint


class PageView(BaseModel):
    """What the page shows after an action."""

    url: str
    title: str
    image: ImagePart | None = None  # a screenshot of the visible part of the page


class Browser(Protocol):
    """One browser page an agent drives. Targets are visible text, `css=<selector>` or `x,y`."""

    async def open(self, url: str) -> PageView: ...
    async def click(self, target: str) -> PageView: ...
    async def type(self, target: str, text: str, submit: bool) -> PageView: ...
    async def scroll(self, pixels: int) -> PageView: ...
    async def back(self) -> PageView: ...
    async def view(self) -> PageView: ...
    async def read(self) -> str: ...
    async def close(self) -> None: ...


class BrowserFactory(Protocol):
    """Starts a fresh browser (no profile, no downloads) for one agent."""

    async def new_browser(self) -> Browser: ...
    async def close(self) -> None: ...
