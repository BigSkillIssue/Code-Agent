"""Everything the core reports goes through the EventBus as one of these events."""

from typing import Annotated, Literal

from pydantic import BaseModel, Field, TypeAdapter

from forge.plan import Plan, Question
from forge.providers.base import Message, ToolCall, ToolResult, Usage


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


EVENT_TYPES: tuple[type[Event], ...] = (
    ModelDelta,
    ModelDone,
    ToolStarted,
    ToolFinished,
    QuestionAsked,
    PlanUpdated,
    StepDone,
    Compacted,
    SessionDone,
    ErrorEvent,
    AgentMessage,
    AgentFinished,
)

AnyEvent = Annotated[
    ModelDelta
    | ModelDone
    | ToolStarted
    | ToolFinished
    | QuestionAsked
    | PlanUpdated
    | StepDone
    | Compacted
    | SessionDone
    | ErrorEvent
    | AgentMessage
    | AgentFinished,
    Field(discriminator="kind"),
]
_EVENT_ADAPTER: TypeAdapter[AnyEvent] = TypeAdapter(AnyEvent)


def parse_event(line: str) -> Event:
    """Parse one JSON line back into its event type."""
    return _EVENT_ADAPTER.validate_json(line)
