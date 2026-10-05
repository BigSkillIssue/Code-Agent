"""MCP client against a stub server over stdio (tests/fixtures/mcp_stub.py)."""

import sys
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from forge.agent import run_agent
from forge.config import ForgeConfig, LimitsConfig, McpServerConfig, PermissionsConfig
from forge.ctx import Ctx
from forge.events import ErrorEvent
from forge.mcp_client import McpHub
from forge.ports import Approval
from forge.providers.base import Capabilities, ToolCall, ToolResult
from forge.providers.fake import FakeProvider, FakeToolCall, FakeTurn
from forge.providers.registry import register_provider
from forge.tools import agent_tools, call_tool
from forge.wiring import close_session, connect_mcp
from support import ScriptedRenderer, drain, make_ctx

STUB = str(Path(__file__).parent / "fixtures" / "mcp_stub.py")


def mcp_cfg(extra_tools: int = 0, **cfg: Any) -> ForgeConfig:
    server = McpServerConfig(command=[sys.executable, STUB, str(extra_tools)])
    config = ForgeConfig(mcp_servers={"stub": server}, roles={"coder": ["fake/coder"]}, **cfg)
    return config


async def open_ctx(root: Path, cfg: ForgeConfig, vision: bool = True, **fields: Any) -> Ctx:
    fake = FakeProvider([], caps=Capabilities(vision=vision))
    register_provider(cfg, fake)
    ctx = make_ctx(root, cfg=cfg, **fields)
    await connect_mcp(ctx)
    return ctx


async def run(ctx: Ctx, name: str, **arguments: Any) -> ToolResult:
    return await call_tool(ctx, ToolCall(id="c1", name=name, arguments=arguments))


@pytest.fixture
async def stub(tmp_project: Path) -> AsyncIterator[Ctx]:
    renderer = ScriptedRenderer()
    ctx = await open_ctx(tmp_project, mcp_cfg(), renderer=renderer)
    yield ctx
    await close_session(ctx)


async def test_stub_server_tools_resources_and_errors(stub: Ctx) -> None:
    hub = stub.state.mcp
    assert isinstance(hub, McpHub) and not hub.deferred
    add = hub.tool("mcp__stub__add")
    assert add is not None and add.group == "mcp" and add.permission == "ask" and not add.read_only
    assert add.spec.description == "Add two numbers and return the sum."
    assert add.spec.parameters["required"] == ["a", "b"]
    assert hub.tool("mcp__stub__lookup") is not None and hub.tool("mcp__stub__lookup").read_only  # type: ignore[union-attr]
    assert "mcp__stub__add" in {t.name for t in agent_tools(stub, "coder")}
    assert "mcp__stub__add" not in {t.name for t in agent_tools(stub, "reviewer")}

    renderer = stub.renderer
    assert isinstance(renderer, ScriptedRenderer)
    result = await run(stub, "mcp__stub__add", a=2, b=3)
    assert result.ok and result.text == "5"
    assert renderer.approval_requests[-1][0].name == "mcp__stub__add"  # asks by default

    assert (await run(stub, "mcp__stub__add", a="two", b=3)).code == "invalid_args"
    assert (await run(stub, "mcp__stub__add", a=2)).code == "invalid_args"
    failed = await run(stub, "mcp__stub__explode")
    assert failed.code == "tool_error" and failed.text.startswith(
        "error[tool_error]: explode reported an error"
    )
    picture = await run(stub, "mcp__stub__picture")
    assert picture.ok and picture.images and picture.images[0].media_type == "image/png"

    listed = await run(stub, "list_mcp_resources")
    assert listed.text == "stub  memo://welcome  welcome  text/plain"
    read = await run(stub, "read_mcp_resource", server="stub", uri="memo://welcome")
    assert read.text.splitlines() == [
        "resource: memo://welcome (text/plain, 27 B)",
        "Welcome to the stub server.",
    ]
    assert (
        await run(stub, "read_mcp_resource", server="stub", uri="memo://nope")
    ).code == "not_found"
    assert (await run(stub, "list_mcp_resources", server="other")).code == "not_found"
    below = await run(stub, "tool_search", query="add numbers")
    assert below.code == "unsupported" and "already loaded" in below.text


async def test_allow_rule_lifts_approval_and_images_without_vision(tmp_project: Path) -> None:
    renderer = ScriptedRenderer(approvals=[Approval(allow=False)])
    cfg = mcp_cfg(permissions=PermissionsConfig(allow=["mcp__stub__*"]))
    ctx = await open_ctx(tmp_project, cfg, vision=False, renderer=renderer)
    try:
        assert (await run(ctx, "mcp__stub__add", a=1, b=1)).text == "2"
        assert renderer.approval_requests == []
        picture = await run(ctx, "mcp__stub__picture")
        assert picture.images == [] and picture.text.startswith("[image: image/png, ")
    finally:
        await close_session(ctx)


async def test_deferred_tools_are_listed_and_loaded_by_search(tmp_project: Path) -> None:
    cfg = mcp_cfg(extra_tools=50, permissions=PermissionsConfig(allow=["mcp__stub__*"]))
    search = FakeToolCall(name="tool_search", arguments={"query": "weather 6", "limit": 2})
    use = FakeToolCall(name="mcp__stub__weather_action_6", arguments={"text": "hi"})
    fake = FakeProvider(
        [FakeTurn(tool_calls=[search]), FakeTurn(tool_calls=[use]), FakeTurn(text="done")]
    )
    register_provider(cfg, fake)
    ctx = make_ctx(tmp_project, cfg=cfg)
    await connect_mcp(ctx)
    try:
        hub = ctx.state.mcp
        assert isinstance(hub, McpHub) and hub.deferred
        result = await run_agent(ctx, "check the weather")
        first = fake.requests[0]
        assert "mcp__stub__weather_action_6: Handle weather request number 6" in first.system
        assert not any(t.name.startswith("mcp__") for t in first.tools)  # names only, no schemas
        assert "tool_search" in {t.name for t in first.tools}
        loaded = result.messages[2].tool_result
        assert loaded is not None and loaded.text.startswith("loaded 2 tools:")
        assert loaded.text.splitlines()[1].startswith("- mcp__stub__weather_action_6: ")
        assert "mcp__stub__weather_action_6" in {t.name for t in fake.requests[1].tools}
        used = result.messages[4].tool_result
        assert used is not None and used.text == "tool 6 got hi"
        nothing = await run(ctx, "tool_search", query="zebra")
        assert (
            nothing.ok
            and nothing.text == 'no deferred tools match "zebra"; available servers: stub'
        )
    finally:
        await close_session(ctx)


async def test_a_broken_server_is_reported_and_skipped(tmp_project: Path) -> None:
    broken = McpServerConfig(command=[sys.executable, "-c", "import sys; sys.exit(3)"])
    cfg = ForgeConfig(mcp_servers={"broken": broken}, limits=LimitsConfig())
    ctx = make_ctx(tmp_project, cfg=cfg)
    events = ctx.bus.subscribe("*")
    await connect_mcp(ctx)
    errors = [e for e in await drain(events) if isinstance(e, ErrorEvent)]
    assert len(errors) == 1 and "MCP server broken is not available" in errors[0].message
    assert ctx.state.mcp is not None and ctx.state.mcp.visible_tools() == []
    await close_session(ctx)
