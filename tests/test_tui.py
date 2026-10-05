"""Textual pilot tests for the terminal UI with a FakeProvider."""

import asyncio
from pathlib import Path

from textual.widgets import Input, OptionList, RichLog, SelectionList, Static

from forge.config import ForgeConfig
from forge.events import PlanUpdated
from forge.local.memory_store import MemoryStore
from forge.local.tui_renderer import ApprovalScreen, QuestionScreen, diff_for
from forge.plan import Plan, Question, Step, TaskSpec
from forge.providers.base import ToolCall
from forge.providers.fake import FakeProvider
from forge.tui import ForgeApp
from forge.wiring import BUILTIN_FAKE, install_fake
from support import NoExecutor

SIZE = (120, 40)


def make_app(tmp_project: Path) -> ForgeApp:
    cfg = ForgeConfig()
    install_fake(cfg, FakeProvider.from_data(BUILTIN_FAKE))
    return ForgeApp(tmp_project, cfg, store=MemoryStore(), executor=NoExecutor())


def stream_text(app: ForgeApp) -> str:
    log = app.query_one("#stream", RichLog)
    return "\n".join("".join(segment.text for segment in line) for line in log.lines)


async def settle(pilot: object, until: object = None, tries: int = 50) -> None:
    for _ in range(tries):
        await pilot.pause()  # type: ignore[attr-defined]
        if until is None or until():  # type: ignore[operator]
            return
        await asyncio.sleep(0.02)


async def test_prompt_streams_the_answer(tmp_project: Path) -> None:
    app = make_app(tmp_project)
    async with app.run_test(size=SIZE) as pilot:
        await settle(pilot, lambda: app.ctx is not None)
        app.query_one("#prompt", Input).value = "say hello"
        await pilot.press("enter")
        await settle(
            pilot, lambda: "Hello from the fake provider." in stream_text(app) and not app.busy
        )
        text = stream_text(app)
        assert "> say hello" in text and "Hello from the fake provider." in text


async def test_approval_dialog_allow_always_and_refuse_with_feedback(tmp_project: Path) -> None:
    app = make_app(tmp_project)
    call = ToolCall(
        id="c1", name="edit_file", arguments={"path": "a.py", "old": "x = 1", "new": "x = 2"}
    )
    async with app.run_test(size=SIZE) as pilot:
        await settle(pilot, lambda: app.ctx is not None)
        waiting = asyncio.create_task(app.renderer.approve(call, "edit_file needs approval"))
        await settle(pilot, lambda: isinstance(app.screen, ApprovalScreen))
        assert app.screen.query("#preview")
        await pilot.press("a")
        approval = await asyncio.wait_for(waiting, 5)
        assert approval.allow and approval.remember

        waiting = asyncio.create_task(app.renderer.approve(call, "again"))
        await settle(pilot, lambda: isinstance(app.screen, ApprovalScreen))
        await pilot.press("n")
        await pilot.press(*"use apply_patch")
        await pilot.press("enter")
        approval = await asyncio.wait_for(waiting, 5)
        assert not approval.allow and approval.feedback == "use apply_patch"


async def test_question_picker_options_other_and_text(tmp_project: Path) -> None:
    app = make_app(tmp_project)
    questions = [
        Question(
            text="Which database?", kind="choice", options=["SQLite", "Postgres"], why="storage"
        ),
        Question(
            text="Which database again?", kind="choice", options=["SQLite", "Postgres"], why="x"
        ),
        Question(text="Project name?", kind="text", default="demo", why="naming"),
        Question(text="Targets?", kind="multi", options=["linux", "mac", "windows"], why="ci"),
    ]
    async with app.run_test(size=SIZE) as pilot:
        await settle(pilot, lambda: app.ctx is not None)
        waiting = asyncio.create_task(app.renderer.ask(questions))

        await settle(pilot, lambda: isinstance(app.screen, QuestionScreen))
        app.screen.query_one("#choices", OptionList).highlighted = 1
        await pilot.press("enter")  # Postgres

        await settle(
            pilot,
            lambda: isinstance(app.screen, QuestionScreen) and app.screen.question is questions[1],
        )
        app.screen.query_one("#choices", OptionList).highlighted = 2  # Other…
        await pilot.press("enter")
        await pilot.press(*"DuckDB", "enter")

        await settle(
            pilot,
            lambda: isinstance(app.screen, QuestionScreen) and app.screen.question is questions[2],
        )
        await pilot.press("enter")  # empty text: the default

        await settle(
            pilot,
            lambda: isinstance(app.screen, QuestionScreen) and app.screen.question is questions[3],
        )
        app.screen.query_one("#choices", SelectionList).select("linux")
        app.screen.query_one("#choices", SelectionList).select("windows")
        await pilot.click("#ok")

        answers = await asyncio.wait_for(waiting, 5)
    assert [a.values for a in answers] == [["Postgres"], ["DuckDB"], ["demo"], ["linux", "windows"]]


async def test_plan_updates_show_in_the_side_panel(tmp_project: Path) -> None:
    app = make_app(tmp_project)
    spec = TaskSpec(goal="g", context="", requirements=[], acceptance_criteria=["ok"], size="small")
    plan = Plan(
        spec=spec,
        steps=[
            Step(id="s1", title="Write the parser", detail="", check="pytest", status="done"),
            Step(id="s2", title="Add tests", detail="", check="pytest", status="doing"),
        ],
    )
    async with app.run_test(size=SIZE) as pilot:
        await settle(pilot, lambda: app.ctx is not None)
        await app.renderer.show(PlanUpdated(session_id="x", ts=0.0, plan=plan))
        await pilot.pause()
        shown = str(app.query_one("#plan", Static).render())
        assert "[x] s1 Write the parser" in shown and "[>] s2 Add tests" in shown


async def test_slash_commands(tmp_project: Path) -> None:
    app = make_app(tmp_project)
    async with app.run_test(size=SIZE) as pilot:
        await settle(pilot, lambda: app.ctx is not None)
        for command in ("/plan", "/mode read-only", "/help"):
            app.query_one("#prompt", Input).value = command
            await pilot.press("enter")
            await pilot.pause()
        text = stream_text(app)
        assert "no plan yet" in text and "sandbox: read-only" in text and "/compact [hard]" in text
        assert app.cfg.sandbox.mode == "read-only"


def test_diff_preview_for_edits() -> None:
    edit = ToolCall(
        id="1", name="edit_file", arguments={"path": "a.py", "old": "x = 1", "new": "x = 2"}
    )
    assert "-x = 1" in diff_for(edit) and "+x = 2" in diff_for(edit)
    write = ToolCall(id="2", name="write_file", arguments={"path": "b.py", "content": "a\nb"})
    assert diff_for(write).splitlines() == ["+++ b.py", "+a", "+b"]
    assert diff_for(ToolCall(id="3", name="bash", arguments={"command": "ls"})) == ""
