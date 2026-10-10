"""Everything the core reports goes through the EventBus as one of these events."""

from typing import Annotated, Literal

from pydantic import BaseModel, Field, TypeAdapter

from forge.plan import Plan, Question
from forge.providers.base import Message, ToolCall, ToolResult, Usage
from forge.todos import Todo


class Event(BaseModel):
    """Base of every event: which session and agent it belongs to, and when."""

    session_id: str
    agent_id: str = "main"
    ts: float  # time.time()


class ModelDelta(Event):
    """A chunk of streamed model text."""

    kind: Literal["model_delta"] = "model_delta"
    text: str


class ModelDone(Event):
    """The model finished one turn."""

    kind: Literal["model_done"] = "model_done"
    message: Message
    usage: Usage


class ToolStarted(Event):
    """A tool call is about to run."""

    kind: Literal["tool_started"] = "tool_started"
    call: ToolCall


class ToolOutput(Event):
    """Output of a running tool so far: whole lines, throttled (S51)."""

    kind: Literal["tool_output"] = "tool_output"
    call_id: str
    text: str


class TodosUpdated(Event):
    """An agent replaced its todo list (S54)."""

    kind: Literal["todos_updated"] = "todos_updated"
    todos: list[Todo]


class ToolFinished(Event):
    """A tool call finished."""

    kind: Literal["tool_finished"] = "tool_finished"
    result: ToolResult


class QuestionAsked(Event):
    """Questions for the user; the answer arrives through the Renderer."""

    kind: Literal["question"] = "question"
    questions: list[Question]


class PlanUpdated(Event):
    """The plan or a step status changed."""

    kind: Literal["plan_updated"] = "plan_updated"
    plan: Plan


class StepDone(Event):
    """A step finished, with or without a passing check."""

    kind: Literal["step_done"] = "step_done"
    step_id: str
    ok: bool


class Compacted(Event):
    """The context was compressed."""

    kind: Literal["compacted"] = "compacted"
    level: int
    tokens_before: int
    tokens_after: int


class SessionDone(Event):
    """The session ended."""

    kind: Literal["session_done"] = "session_done"
    ok: bool
    report: str


class ErrorEvent(Event):
    """Something went wrong that the user should see."""

    kind: Literal["error"] = "error"
    message: str


class AgentMessage(Event):
    """A message between agents (send_message); agent_id is the sender."""

    kind: Literal["agent_message"] = "agent_message"
    to: str
    summary: str
    text: str


class AgentFinished(Event):
    """A background sub-agent ended; its report went to the parent's inbox."""

    kind: Literal["agent_finished"] = "agent_finished"
    role: str
    status: str
    report: str


GuidelineArea = Literal["safety", "performance", "business", "design", "legal", "hig"]
GuidelineStatus = Literal["ok", "concern", "violation"]


class GuidelineFinding(BaseModel):
    """The Apple reviewer's judgement of one guideline area (S59)."""

    area: GuidelineArea  # App Store Review Guidelines sections 1-5, or the HIG
    status: GuidelineStatus
    guideline: str = ""  # the rule's number or HIG page, e.g. "5.1.1" or "HIG Accessibility"
    reason: str
    fix: str = ""  # what would make it ok


class GuidelineReview(Event):
    """The Apple reviewer's verdict on the request, the plan or the finished app (S59), or on
    its App Store listing (S61)."""

    kind: Literal["guideline_review"] = "guideline_review"
    stage: Literal["prompt", "plan", "product", "listing"]
    verdict: GuidelineStatus  # the worst finding; "concern" when the review failed
    summary: str
    findings: list[GuidelineFinding] = []
    sources: list[str] = []  # the Apple pages the reviewer read
    error: str = ""  # set when no review could be made: that is never a pass


EVENT_TYPES: tuple[type[Event], ...] = (
    ModelDelta,
    ModelDone,
    ToolStarted,
    ToolOutput,
    ToolFinished,
    TodosUpdated,
    QuestionAsked,
    PlanUpdated,
    StepDone,
    Compacted,
    SessionDone,
    ErrorEvent,
    AgentMessage,
    AgentFinished,
    GuidelineReview,
)

AnyEvent = Annotated[
    ModelDelta
    | ModelDone
    | ToolStarted
    | ToolOutput
    | ToolFinished
    | TodosUpdated
    | QuestionAsked
    | PlanUpdated
    | StepDone
    | Compacted
    | SessionDone
    | ErrorEvent
    | AgentMessage
    | AgentFinished
    | GuidelineReview,
    Field(discriminator="kind"),
]
_EVENT_ADAPTER: TypeAdapter[AnyEvent] = TypeAdapter(AnyEvent)


def parse_event(line: str) -> Event:
    """Parse one JSON line back into its event type."""
    return _EVENT_ADAPTER.validate_json(line)
