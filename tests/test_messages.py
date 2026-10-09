"""Round-trip tests for the message and event models."""

import pytest
from pydantic import BaseModel

from forge import events
from forge.plan import Plan, Question, Step, TaskSpec
from forge.providers.base import (
    ImagePart,
    Message,
    TextPart,
    ToolCall,
    ToolResult,
    Usage,
)

SPEC = TaskSpec(
    goal="Add a --verbose flag",
    context="cli.py parses flags with argparse",
    requirements=["flag sets log level"],
    acceptance_criteria=["forge --verbose prints debug logs"],
    size="small",
)
QUESTION = Question(text="Which level?", kind="choice", options=["DEBUG", "INFO"], why="sets noise")
PLAN = Plan(spec=SPEC, steps=[Step(id="s1", title="Add flag", detail="argparse", check="pytest")])
CALL = ToolCall(id="call_1", name="read_file", arguments={"path": "src/app.py", "limit": 10})
RESULT = ToolResult(
    call_id="call_1", ok=False, text="error[not_found]: no such file", code="not_found"
)
MESSAGE = Message(role="assistant", parts=[TextPart(text="Reading.\nNow.")], tool_calls=[CALL])
USAGE = Usage(input_tokens=10, output_tokens=5, cached_tokens=2, cost_usd=0.001)

MODELS: list[BaseModel] = [
    TextPart(text="hi"),
    ImagePart(media_type="image/png", data_b64="aGk="),
    CALL,
    RESULT,
    ToolResult(
        call_id="c2", ok=True, text="ok", images=[ImagePart(media_type="image/png", data_b64="")]
    ),
    MESSAGE,
    Message(role="tool", tool_result=RESULT),
    Message(
        role="user", parts=[TextPart(text="x"), ImagePart(media_type="image/jpeg", data_b64="")]
    ),
    USAGE,
]

EVENTS: list[events.Event] = [
    events.ModelDelta(session_id="s", ts=1.0, text="multi\nline"),
    events.ModelDone(session_id="s", ts=1.0, message=MESSAGE, usage=USAGE),
    events.ToolStarted(session_id="s", agent_id="a1", ts=1.0, call=CALL),
    events.ToolOutput(session_id="s", agent_id="a1", ts=1.0, call_id="c1", text="line 1\n"),
    events.ToolFinished(session_id="s", ts=1.0, result=RESULT),
    events.TodosUpdated(
        session_id="s", ts=1.0, todos=[events.Todo(content="Run tests", status="pending")]
    ),
    events.QuestionAsked(session_id="s", ts=1.0, questions=[QUESTION]),
    events.PlanUpdated(session_id="s", ts=1.0, plan=PLAN),
    events.StepDone(session_id="s", ts=1.0, step_id="s1", ok=True),
    events.Compacted(session_id="s", ts=1.0, level=2, tokens_before=9000, tokens_after=1200),
    events.SessionDone(session_id="s", ts=1.0, ok=True, report="all done\nreport"),
    events.ErrorEvent(session_id="s", ts=1.0, message="boom"),
    events.AgentMessage(session_id="s", ts=1.0, agent_id="a1", to="a2", summary="hi", text="hi"),
    events.AgentFinished(
        session_id="s", ts=1.0, agent_id="a2", role="tester", status="done", report="ok"
    ),
    events.GuidelineReview(
        session_id="s",
        ts=1.0,
        agent_id="apple-reviewer-plan",
        stage="plan",
        verdict="concern",
        summary="Login needs Sign in with Apple.",
        sources=["https://developer.apple.com/"],
        findings=[
            events.GuidelineFinding(
                area="design", status="concern", guideline="4.8", reason="Google login"
            )
        ],
    ),
]


@pytest.mark.parametrize("model", MODELS, ids=lambda m: type(m).__name__)
def test_message_models_round_trip(model: BaseModel) -> None:
    again = type(model).model_validate_json(model.model_dump_json())
    assert again == model


@pytest.mark.parametrize("event", EVENTS, ids=lambda e: type(e).__name__)
def test_event_is_one_json_line_and_parses_back(event: events.Event) -> None:
    line = event.model_dump_json()
    assert "\n" not in line
    parsed = events.parse_event(line)
    assert type(parsed) is type(event)
    assert parsed == event


def test_every_event_kind_is_unique() -> None:
    kinds = [e.model_dump()["kind"] for e in EVENTS]
    assert len(kinds) == len(set(kinds)) == len(events.EVENT_TYPES)


def test_tool_call_message_survives_round_trip_unchanged() -> None:
    dumped = MESSAGE.model_dump_json()
    again = Message.model_validate_json(dumped)
    assert again.tool_calls[0].arguments == {"path": "src/app.py", "limit": 10}
    assert again.model_dump_json() == dumped


def test_message_text_joins_text_parts() -> None:
    msg = Message(
        role="user",
        parts=[TextPart(text="a"), ImagePart(media_type="x", data_b64=""), TextPart(text="b")],
    )
    assert msg.text() == "a\nb"


def test_usage_adds_up() -> None:
    total = USAGE + Usage(input_tokens=1, output_tokens=1, cost_usd=0.5)
    assert (total.input_tokens, total.output_tokens, total.cached_tokens) == (11, 6, 2)
    assert total.cost_usd == pytest.approx(0.501)


def test_unknown_event_kind_is_rejected() -> None:
    with pytest.raises(ValueError):
        events.parse_event('{"session_id": "s", "ts": 1.0, "kind": "nope"}')
