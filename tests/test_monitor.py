"""The monitor tool: new output lines of a background command arrive as messages (S53)."""

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from forge.agent import run_agent
from forge.config import ForgeConfig, PermissionsConfig, SandboxConfig
from forge.ctx import Ctx
from forge.local.local_executor import LocalExecutor
from forge.providers.base import ToolCall, ToolResult
from forge.providers.fake import FakeProvider, FakeToolCall, FakeTurn
from forge.providers.registry import register_provider
from forge.runtime.shell import find_shell
from forge.team import AgentRegistry
from forge.tools import call_tool
from support import make_ctx

pytestmark = pytest.mark.skipif(find_shell("bash") is None, reason="the commands are bash")


@pytest.fixture
async def mctx(tmp_project: Path) -> AsyncIterator[Ctx]:
    executor = LocalExecutor(tmp_project)
    cfg = ForgeConfig(
        sandbox=SandboxConfig(mode="full-access"),
        permissions=PermissionsConfig(deny=["bash(rm *)"]),
    )
    ctx = make_ctx(tmp_project, cfg=cfg, executor=executor)
    yield ctx
    await ctx.state.monitors.close()
    await executor.close()


async def run(ctx: Ctx, name: str, **arguments: Any) -> ToolResult:
    return await call_tool(ctx, ToolCall(id="c1", name=name, arguments=arguments))


def team(ctx: Ctx) -> AgentRegistry:
    assert isinstance(ctx.state.team, AgentRegistry)
    return ctx.state.team


async def messages_until(ctx: Ctx, word: str) -> str:
    """Collect inbox messages of the main agent until one contains `word`."""
    seen: list[str] = []
    while not any(word in text for text in seen):
        seen += await team(ctx).wait_for_message("main")
    return "\n".join(seen)


async def test_new_lines_arrive_as_messages(mctx: Ctx) -> None:
    started = await run(
        mctx,
        "monitor",
        command="for i in 1 2 3; do echo tick $i; sleep 0.3; done",
        description="ticks",
    )
    assert started.ok and started.text.startswith("monitor m1 started")
    assert mctx.state.monitors.active("main")
    text = await messages_until(mctx, "ended")
    assert "[monitor m1: ticks]" in text
    assert [line for line in text.splitlines() if line.startswith("tick")] == [
        "tick 1",
        "tick 2",
        "tick 3",
    ]
    assert "exit code 0" in text and not mctx.state.monitors.active("main")


async def test_filter_keeps_only_matching_lines(mctx: Ctx) -> None:
    await run(
        mctx,
        "monitor",
        command="echo ok; echo 'ERROR disk full'; echo ok",
        description="errors",
        filter="ERROR",
    )
    text = await messages_until(mctx, "ended")
    assert "ERROR disk full" in text and "\nok" not in text


async def test_stop_ends_the_command(mctx: Ctx) -> None:
    await run(mctx, "monitor", command="while true; do echo x; sleep 0.2; done", description="loop")
    stopped = await run(mctx, "monitor_stop", monitor_id="m1")
    assert stopped.ok and "stopped" in stopped.text
    assert not mctx.state.monitors.active("main")
    again = await run(mctx, "monitor_stop", monitor_id="m1")
    assert again.code == "invalid_args"


async def test_timeout_stops_the_command(mctx: Ctx) -> None:
    await run(mctx, "monitor", command="sleep 30", description="slow", timeout_s=1)
    text = await messages_until(mctx, "timed out")
    assert "[monitor m1 timed out after 1s" in text


async def test_bash_rules_apply(mctx: Ctx) -> None:
    result = await run(mctx, "monitor", command="rm -rf build", description="clean")
    assert result.code == "permission_denied"


async def test_bad_filter_is_refused(mctx: Ctx) -> None:
    result = await run(mctx, "monitor", command="echo hi", description="d", filter="(")
    assert result.code == "invalid_args"


async def test_the_agent_waits_for_monitor_messages(tmp_project: Path) -> None:
    executor = LocalExecutor(tmp_project)
    fake = FakeProvider(
        [
            FakeTurn(
                tool_calls=[
                    FakeToolCall(
                        name="monitor",
                        arguments={
                            "command": "sleep 0.3; echo build done",
                            "description": "build",
                        },
                    )
                ]
            ),
            FakeTurn(text="Waiting for the build."),
            FakeTurn(
                tool_calls=[FakeToolCall(name="monitor_stop", arguments={"monitor_id": "m1"})]
            ),
            FakeTurn(text="The build is done."),
        ]
    )
    cfg = ForgeConfig(roles={"coder": ["fake/coder"]}, sandbox=SandboxConfig(mode="full-access"))
    register_provider(cfg, fake)
    ctx = make_ctx(tmp_project, cfg=cfg, executor=executor)
    try:
        result = await run_agent(ctx, "build and tell me")
    finally:
        await ctx.state.monitors.close()
        await executor.close()
    assert result.text == "The build is done."
    woken = fake.requests[2]  # the turn after the agent waited
    assert "build done" in "\n".join(m.text() for m in woken.messages)
