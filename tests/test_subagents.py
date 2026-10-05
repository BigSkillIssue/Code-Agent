"""Foreground sub-agents (docs/TOOLS.md: spawn_agent)."""

from pathlib import Path
from typing import Any

from forge.agent import run_agent
from forge.config import ForgeConfig, LimitsConfig
from forge.ctx import Ctx
from forge.providers.base import ToolCall, ToolResult
from forge.providers.fake import FakeProvider, FakeToolCall, FakeTurn
from forge.providers.registry import register_provider
from forge.team import AgentRegistry
from forge.tools import call_tool
from support import make_ctx

BIG = "SECRET-DETAIL " * 500


def team_ctx(
    root: Path, roles: dict[str, list[FakeTurn]], **limits: Any
) -> tuple[Ctx, FakeProvider]:
    fake = FakeProvider(roles=roles)
    cfg = ForgeConfig(
        roles={role: [f"fake/{role}"] for role in roles}, limits=LimitsConfig(**limits)
    )
    register_provider(cfg, fake)
    return make_ctx(root, cfg=cfg), fake


def spawn(role: str, task: str, **extra: Any) -> FakeTurn:
    return FakeTurn(
        tool_calls=[
            FakeToolCall(name="spawn_agent", arguments={"role": role, "task": task, **extra})
        ]
    )


def registry(ctx: Ctx) -> AgentRegistry:
    assert isinstance(ctx.state.team, AgentRegistry)
    return ctx.state.team


async def test_parent_context_grows_only_by_the_report(tmp_project: Path) -> None:
    (tmp_project / "big.txt").write_text(BIG)
    ctx, fake = team_ctx(
        tmp_project,
        {
            "coder": [spawn("explore", "Where is SECRET used?"), FakeTurn(text="All done.")],
            "explore": [
                FakeTurn(
                    tool_calls=[FakeToolCall(name="read_file", arguments={"path": "big.txt"})]
                ),
                FakeTurn(text="Found it: big.txt:1"),
            ],
        },
    )
    result = await run_agent(ctx, "find the secret")
    assert result.stopped == "done" and result.text == "All done."
    tool_result = result.messages[2].tool_result
    assert tool_result is not None and tool_result.ok
    lines = tool_result.text.splitlines()
    assert lines[0].startswith("agent a1 (explore) finished: done, 2 turns")
    assert lines[1:] == ["--- report ---", "Found it: big.txt:1"]
    parent_text = "\n".join(
        m.text() + (m.tool_result.text if m.tool_result else "") for m in result.messages
    )
    assert "SECRET-DETAIL" not in parent_text
    child = registry(ctx).agents["a1"]
    assert child.status == "done" and any(
        m.tool_result and "SECRET-DETAIL" in m.tool_result.text for m in child.messages
    )
    explore_request = next(r for r in fake.requests if r.model == "explore")
    assert "explore sub-agent" in explore_request.system
    session_text = "\n".join(m.tool_result.text for m in ctx.session.messages if m.tool_result)
    assert "SECRET-DETAIL" not in session_text  # the session transcript is the lead's


async def test_reviewer_child_cannot_edit(tmp_project: Path) -> None:
    (tmp_project / "a.py").write_text("x = 1\n")
    edit = FakeToolCall(
        name="edit_file", arguments={"path": "a.py", "old": "x = 1", "new": "x = 2"}
    )
    ctx, fake = team_ctx(
        tmp_project,
        {
            "coder": [spawn("reviewer", "Review a.py"), FakeTurn(text="ok")],
            "reviewer": [FakeTurn(tool_calls=[edit]), FakeTurn(text="Looks fine.")],
        },
    )
    await run_agent(ctx, "review")
    reviewer_request = next(r for r in fake.requests if r.model == "reviewer")
    names = {t.name for t in reviewer_request.tools}
    assert "edit_file" not in names and "read_file" in names
    assert "ask_user" not in names and "spawn_agent" not in names
    child = registry(ctx).agents["a1"]
    refused = next(m.tool_result for m in child.messages if m.tool_result)
    assert refused is not None and refused.code == "unsupported"
    assert (tmp_project / "a.py").read_text() == "x = 1\n"
    assert "Your role: reviewer." in reviewer_request.system


async def test_max_turns_gives_a_partial_report(tmp_project: Path) -> None:
    look = FakeTurn(text="still looking", tool_calls=[FakeToolCall(name="list_dir", arguments={})])
    ctx, _ = team_ctx(
        tmp_project,
        {
            "coder": [spawn("explore", "search", max_turns=2), FakeTurn(text="ok")],
            "explore": [look, look, look],
        },
    )
    result = await run_agent(ctx, "go")
    tool_result = result.messages[2].tool_result
    assert tool_result is not None
    assert (
        tool_result.text.splitlines()[0] == "agent a1 (explore) stopped: max_turns (partial report)"
    )
    assert "still looking" in tool_result.text
    assert registry(ctx).agents["a1"].status == "stopped"


async def test_children_cannot_ask_the_user_or_spawn(tmp_project: Path) -> None:
    ask = FakeToolCall(name="ask_user", arguments={"questions": []})
    nested = FakeToolCall(name="spawn_agent", arguments={"role": "explore", "task": "x"})
    ctx, _ = team_ctx(
        tmp_project,
        {
            "coder": [spawn("tester", "write tests"), FakeTurn(text="ok")],
            "tester": [FakeTurn(tool_calls=[ask, nested]), FakeTurn(text="done")],
        },
    )
    await run_agent(ctx, "go")
    child = registry(ctx).agents["a1"]
    codes = [m.tool_result.code for m in child.messages if m.tool_result]
    assert codes == ["unsupported", "unsupported"]
    assert set(registry(ctx).agents) == {"main", "a1"}  # the nested spawn created nothing


async def run(ctx: Ctx, **arguments: Any) -> ToolResult:
    return await call_tool(ctx, ToolCall(id="c1", name="spawn_agent", arguments=arguments))


async def test_spawn_checks_role_name_and_limits(tmp_project: Path) -> None:
    ctx, _ = team_ctx(tmp_project, {"coder": []}, max_parallel_agents=1)
    assert (await run(ctx, role="wizard", task="x")).code == "not_found"
    assert (await run(ctx, role="explore", task="x", name="Bad Name")).code == "invalid_args"
    assert (await run(ctx, role="explore", task="x", max_turns=0)).code == "invalid_args"
    registry(ctx).add("explore", "busy", None, "main")  # one agent already running
    assert (await run(ctx, role="explore", task="x")).code == "limit_reached"
