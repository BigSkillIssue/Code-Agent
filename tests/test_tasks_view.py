"""One view of everything running in the background: jobs, agents, monitors (S55)."""

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from forge.local.tasks_screen import TasksScreen
from textual.widgets import OptionList, Static

from forge.commands import handle_command
from forge.config import ForgeConfig, SandboxConfig
from forge.ctx import Ctx
from forge.local.local_executor import LocalExecutor
from forge.providers.base import ToolCall
from forge.runtime.shell import find_shell
from forge.tasks_view import list_tasks
from forge.team import AgentRegistry
from forge.tools import call_tool
from support import make_ctx
from test_tui import SIZE, make_app, settle

needs_bash = pytest.mark.skipif(find_shell("bash") is None, reason="the commands are bash")


@pytest.fixture
async def busy(tmp_project: Path) -> AsyncIterator[Ctx]:
    """A session with a background job, a monitor and a running sub-agent."""
    executor = LocalExecutor(tmp_project)
    cfg = ForgeConfig(sandbox=SandboxConfig(mode="full-access"))
    ctx = make_ctx(tmp_project, cfg=cfg, executor=executor)
    await run(
        ctx, "bash", command="echo serving; sleep 30", background=True, description="dev server"
    )
    await run(ctx, "monitor", command="sleep 30", description="watch logs")
    team = ctx.state.team
    assert isinstance(team, AgentRegistry)
    team.add("tester", "Run the API tests\nand report", None, "main")
    yield ctx
    await ctx.state.monitors.close()
    await executor.close()


async def run(ctx: Ctx, name: str, **arguments: Any) -> None:
    result = await call_tool(ctx, ToolCall(id="c1", name=name, arguments=arguments))
    assert result.ok, result.text


@needs_bash
async def test_list_shows_jobs_agents_and_monitors(busy: Ctx) -> None:
    rows = {row.id: row for row in await list_tasks(busy)}
    assert set(rows) == {"j1", "m1", "a1"}  # the monitor's own job j2 is shown as m1
    assert (rows["j1"].kind, rows["j1"].description, rows["j1"].status) == (
        "job",
        "dev server",
        "running",
    )
    assert rows["m1"].kind == "monitor" and rows["m1"].description == "watch logs"
    assert rows["a1"].kind == "agent" and rows["a1"].description == "tester: Run the API tests"


@needs_bash
async def test_tasks_command_lists_shows_and_stops(busy: Ctx) -> None:
    listing = (await handle_command(busy, "/tasks")).text
    assert "j1" in listing and "m1" in listing and "a1" in listing
    detail = (await handle_command(busy, "/tasks j1")).text
    assert "serving" in detail
    stopped = (await handle_command(busy, "/tasks stop j1")).text
    assert "stopped j1" in stopped
    stopped = (await handle_command(busy, "/tasks stop m1")).text
    assert "stopped m1" in stopped and not busy.state.monitors.active("main")
    rows = {row.id: row.status for row in await list_tasks(busy)}
    assert rows["j1"] != "running" and rows["m1"] == "stopped"
    unknown = (await handle_command(busy, "/tasks stop x9")).text
    assert "no task x9" in unknown


async def test_tui_shows_running_tasks_and_opens_the_list(tmp_project: Path) -> None:
    app = make_app(tmp_project)
    async with app.run_test(size=SIZE) as pilot:
        await settle(pilot, lambda: app.ctx is not None)
        assert app.ctx is not None and isinstance(app.ctx.state.team, AgentRegistry)
        app.ctx.state.team.add("researcher", "Compare the HTTP clients", None, "main")
        bar = app.query_one("#tasksbar", Static)
        await settle(pilot, lambda: "1 background task" in str(bar.render()), tries=100)
        await pilot.press("ctrl+t")
        await settle(pilot, lambda: isinstance(app.screen, TasksScreen))
        rows = app.screen.query_one(OptionList)
        assert "a1" in str(rows.get_option_at_index(0).prompt)
