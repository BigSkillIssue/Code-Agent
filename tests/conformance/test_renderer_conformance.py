"""Every non-interactive Renderer must show any event and answer questions and approvals."""

import io
import json

import pytest
from rich.console import Console

from forge.local.auto_renderer import AutoRenderer
from forge.local.json_renderer import JsonRenderer
from forge.local.rich_renderer import RichRenderer
from forge.plan import Question
from forge.ports import Renderer
from forge.providers.base import ToolCall
from test_messages import EVENTS


def make(kind: str) -> Renderer:
    if kind == "auto":
        return AutoRenderer()
    if kind == "json":
        return JsonRenderer(io.StringIO(), auto_approve=True)
    return RichRenderer(Console(file=io.StringIO(), width=100), auto_approve=True)


@pytest.fixture(params=["auto", "json", "rich"])
def renderer(request: pytest.FixtureRequest) -> Renderer:
    return make(request.param)


async def test_shows_every_event(renderer: Renderer) -> None:
    for event in EVENTS:
        await renderer.show(event)
    if isinstance(renderer, JsonRenderer):
        out = renderer.out.getvalue()  # type: ignore[attr-defined]
        assert all(json.loads(line)["kind"] for line in out.splitlines())


async def test_answers_one_per_question(renderer: Renderer) -> None:
    questions = [
        Question(text="Database?", kind="choice", options=["a", "b"], default="b", why="x"),
        Question(text="Name?", kind="text", default="demo", why="y"),
    ]
    answers = await renderer.ask(questions)
    assert [a.question_index for a in answers] == [0, 1]
    assert [a.values for a in answers] == [["b"], ["demo"]]


async def test_approves_when_told_to(renderer: Renderer) -> None:
    approval = await renderer.approve(
        ToolCall(id="1", name="bash", arguments={"command": "ls"}), "why"
    )
    assert approval.allow
