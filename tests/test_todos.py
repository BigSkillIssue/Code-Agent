"""The agent's todo list: todo_write, the todos_updated event and how it is shown (S54)."""

import io
import json
from dataclasses import replace
from pathlib import Path
from typing import Any

from rich.console import Console
from textual.widgets import Static

from forge import prompts
from forge.ctx import Ctx
from forge.events import Todo, TodosUpdated
from forge.local.json_renderer import JsonRenderer
from forge.local.rich_renderer import RichRenderer
from forge.providers.base import ToolCall, ToolResult
from forge.tools import call_tool
from support import drain
from test_tui import SIZE, make_app, settle

TODOS = [
    {"content": "Read the parser", "status": "completed", "active_form": "Reading the parser"},
    {"content": "Fix the leap year check", "status": "in_progress", "active_form": "Fixing it"},
    {"content": "Run the tests", "status": "pending", "active_form": "Running the tests"},
]


async def write(ctx: Ctx, todos: list[dict[str, Any]]) -> ToolResult:
    return await call_tool(ctx, ToolCall(id="c1", name="todo_write", arguments={"todos": todos}))


async def test_todo_write_keeps_the_list_and_publishes_it(ctx: Ctx) -> None:
    events = ctx.bus.subscribe("*")
    result = await write(ctx, TODOS)
    assert result.ok
    assert result.text.splitlines() == [
        "todos: 1 of 3 done",
        "[x] Read the parser",
        "[>] Fixing it",
        "[ ] Run the tests",
    ]
    assert [t.content for t in ctx.state.todos["main"]] == [t["content"] for t in TODOS]
    published = [e for e in await drain(events) if isinstance(e, TodosUpdated)]
    assert len(published) == 1 and published[0].todos[1].status == "in_progress"


async def test_only_one_todo_may_be_in_progress(ctx: Ctx) -> None:
    both = [dict(TODOS[1]), {**TODOS[2], "status": "in_progress"}]
    result = await write(ctx, both)
    assert result.code == "invalid_args" and "in_progress" in result.text


async def test_each_agent_has_its_own_todo_list(ctx: Ctx) -> None:
    await write(ctx, TODOS)
    child = replace(ctx, agent_id="a1", role="tester")
    await write(child, TODOS[2:])
    assert len(ctx.state.todos["main"]) == 3 and len(ctx.state.todos["a1"]) == 1


async def test_json_and_plain_output_show_the_todo_list() -> None:
    event = TodosUpdated(session_id="s", agent_id="main", ts=1.0, todos=[Todo(**t) for t in TODOS])
    out = io.StringIO()
    await JsonRenderer(out, auto_approve=True).show(event)
    assert json.loads(out.getvalue())["kind"] == "todos_updated"
    console = Console(file=io.StringIO(), width=100)
    await RichRenderer(console, auto_approve=True).show(event)
    shown = console.file.getvalue()  # type: ignore[attr-defined]
    assert "[x] Read the parser" in shown and "[>] Fixing it" in shown


def test_the_prompt_asks_for_todo_lists() -> None:
    assert "todo_write" in prompts.TOOL_RULES


async def test_tui_shows_the_todo_list(tmp_project: Path) -> None:
    app = make_app(tmp_project)
    async with app.run_test(size=SIZE) as pilot:
        await settle(pilot, lambda: app.ctx is not None)
        assert app.ctx is not None
        await write(app.ctx, TODOS)
        panel = app.query_one("#todos", Static)
        await settle(pilot, lambda: "Fixing it" in str(panel.render()))
        assert "[x] Read the parser" in str(panel.render())
