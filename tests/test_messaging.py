"""Background agents, messages, list_agents and stop_agent."""

import asyncio
import time
from pathlib import Path
from typing import Any

from forge.agent import run_agent
from forge.config import ForgeConfig
from forge.ctx import Ctx
from forge.events import AgentFinished
from forge.ports import Command, CommandResult, SandboxPolicy
from forge.providers.base import ToolCall, ToolResult
from forge.providers.fake import FakeProvider, FakeToolCall, FakeTurn
from forge.providers.registry import register_provider
from forge.team import AgentRegistry
from forge.tools import call_tool
from support import drain, make_ctx


def call(tool: str, **arguments: Any) -> FakeToolCall:
    return FakeToolCall(name=tool, arguments=arguments)


def team_ctx(
    root: Path, roles: dict[str, list[FakeTurn]], **ctx_fields: Any
) -> tuple[Ctx, FakeProvider]:
    fake = FakeProvider(roles=roles)
    cfg = ForgeConfig(roles={role: [f"fake/{role}"] for role in roles})
    register_provider(cfg, fake)
    return make_ctx(root, cfg=cfg, **ctx_fields), fake


def registry(ctx: Ctx) -> AgentRegistry:
    assert isinstance(ctx.state.team, AgentRegistry)
    return ctx.state.team


async def run(ctx: Ctx, name: str, **arguments: Any) -> ToolResult:
    return await call_tool(ctx, ToolCall(id="c1", name=name, arguments=arguments))


async def test_two_background_agents_exchange_a_message_and_both_finish(tmp_project: Path) -> None:
    look = FakeTurn(tool_calls=[call("list_dir")])
    ctx, fake = team_ctx(
        tmp_project,
        {
            "coder": [
                FakeTurn(
                    tool_calls=[
                        call(
                            "spawn_agent",
                            role="explore",
                            task="find",
                            background=True,
                            name="alpha",
                        ),
                        call(
                            "spawn_agent", role="tester", task="test", background=True, name="beta"
                        ),
                    ]
                ),
                FakeTurn(text="waiting for reports"),
                FakeTurn(text="waiting for reports"),
                FakeTurn(text="all done"),
            ],
            # alpha looks around first, so beta exists by the time it writes
            "explore": [
                look,
                look,
                FakeTurn(tool_calls=[call("send_message", to="beta", text="hi beta")]),
                FakeTurn(text="alpha done"),
            ],
            "tester": [look, look, look, look, look, look, FakeTurn(text="beta done")],
        },
    )
    events = ctx.bus.subscribe("*")
    result = await asyncio.wait_for(run_agent(ctx, "coordinate"), 10)
    started = [m.tool_result.text for m in result.messages if m.tool_result][:2]
    assert (
        started[0]
        == "started agent a1 (explore) in background; you will receive its report as a message"
    )
    team = registry(ctx)
    assert team.agents["a1"].status == "done" and team.agents["a2"].status == "done"
    tester_requests = [r for r in fake.requests if r.model == "tester"]
    assert any(
        "[message from a1 (explore)]: hi beta" in m.text()
        for r in tester_requests
        for m in r.messages
    )
    inbox = [m.text() for m in result.messages if m.role == "user"]
    assert any(t.startswith("[agent a1 (explore) finished: done]\nalpha done") for t in inbox)
    assert any(t.startswith("[agent a2 (tester) finished: done]\nbeta done") for t in inbox)
    assert result.text == "all done"
    finished = [e for e in await drain(events) if isinstance(e, AgentFinished)]
    assert {e.agent_id for e in finished} == {"a1", "a2"}


async def test_send_message_rules(tmp_project: Path) -> None:
    ctx, _ = team_ctx(tmp_project, {"coder": []})
    team = registry(ctx)
    worker = team.add("tester", "t", "api-tests", "main")
    assert (
        await run(ctx, "send_message", to="api-tests", text="hello")
    ).text == "delivered to a1 (tester)"
    assert (await run(ctx, "send_message", to="a1", text="again")).ok
    assert team.take_messages("a1") == [
        "[message from main (coder)]: hello",
        "[message from main (coder)]: again",
    ]
    assert (await run(ctx, "send_message", to="main", text="me")).code == "invalid_args"
    assert (await run(ctx, "send_message", to="a1", text=" ")).code == "invalid_args"
    assert (await run(ctx, "send_message", to="nobody", text="x")).code == "not_found"
    worker.status = "done"
    finished = await run(ctx, "send_message", to="a1", text="late")
    assert finished.code == "not_found" and "has finished" in finished.text


async def test_list_agents_shows_status_and_tokens(tmp_project: Path) -> None:
    ctx, _ = team_ctx(
        tmp_project,
        {
            "coder": [
                FakeTurn(
                    tool_calls=[
                        call("spawn_agent", role="explore", task="Find where tokens are validated")
                    ]
                ),
                FakeTurn(text="ok"),
            ],
            "explore": [FakeTurn(text="found")],
        },
    )
    await run_agent(ctx, "Add JWT authentication")
    table = (await run(ctx, "list_agents")).text.splitlines()
    assert table[0].split() == ["id", "name", "role", "status", "turns", "tokens", "task"]
    assert table[1].split()[:4] == ["main", "-", "coder", "running"]
    assert table[1].endswith("Add JWT authentication")
    assert table[2].split()[:5] == ["a1", "-", "explore", "done", "1"]
    assert table[2].endswith("Find where tokens are validated")


class SlowExecutor:
    """Commands take long; background commands become jobs that can be stopped."""

    def __init__(self) -> None:
        self.stopped: list[str] = []

    async def run(
        self, cmd: Command, policy: SandboxPolicy, background: bool = False
    ) -> CommandResult:
        if background:
            return CommandResult(exit_code=None, stdout="", stderr="", job_id="j1", pid=1)
        await asyncio.sleep(30)
        return CommandResult(exit_code=0, stdout="", stderr="")

    async def job_output(self, job_id: str, since_line: int = 0) -> CommandResult:
        return CommandResult(exit_code=None, stdout="", stderr="", job_id=job_id)

    async def job_stop(self, job_id: str) -> CommandResult:
        self.stopped.append(job_id)
        return CommandResult(exit_code=-15, stdout="", stderr="SIGTERM", job_id=job_id)


async def test_stop_agent_cancels_it_and_its_jobs(tmp_project: Path) -> None:
    executor = SlowExecutor()
    ctx, _ = team_ctx(
        tmp_project,
        {
            "coder": [],
            "tester": [
                FakeTurn(tool_calls=[call("bash", command="npm run dev", background=True)]),
                FakeTurn(tool_calls=[call("bash", command="sleep 30")]),
            ],
        },
        executor=executor,
    )
    started = await run(ctx, "spawn_agent", role="tester", task="run the server", background=True)
    assert started.ok
    team = registry(ctx)
    for _ in range(100):
        await asyncio.sleep(0.01)
        if team.agents["a1"].jobs:
            break
    began = time.monotonic()
    stopped = await run(ctx, "stop_agent", agent_id="a1")
    assert time.monotonic() - began < 1.5
    assert (
        stopped.ok
        and stopped.text.startswith("stopped a1 (tester) after ")
        and "stopped jobs j1" in stopped.text
    )
    assert executor.stopped == ["j1"] and team.agents["a1"].status == "cancelled"
    again = await run(ctx, "stop_agent", agent_id="a1")
    assert again.code == "invalid_args" and "cancelled" in again.text
