"""The research tool (a researcher sub-agent) and the web search fallback backend."""

from dataclasses import replace
from pathlib import Path

import httpx
import pytest
import respx

from forge import prompts
from forge.agent import run_agent
from forge.config import ForgeConfig, WebConfig
from forge.ctx import Ctx
from forge.providers.base import ToolCall
from forge.providers.fake import FakeProvider, FakeToolCall, FakeTurn
from forge.providers.registry import register_provider
from forge.runtime import web
from forge.team import AgentRegistry
from forge.tools import agent_tools, call_tool
from support import make_ctx


def research_ctx(root: Path, roles: dict[str, list[FakeTurn]]) -> tuple[Ctx, FakeProvider]:
    fake = FakeProvider(roles=roles)
    cfg = ForgeConfig(roles={role: [f"fake/{role}"] for role in roles})
    register_provider(cfg, fake)
    ctx = make_ctx(root, cfg=cfg)
    ctx.state.mode = "solo"
    return ctx, fake


def research(question: str, **extra: object) -> FakeTurn:
    return FakeTurn(
        tool_calls=[FakeToolCall(name="research", arguments={"question": question, **extra})]
    )


def registry(ctx: Ctx) -> AgentRegistry:
    assert isinstance(ctx.state.team, AgentRegistry)
    return ctx.state.team


async def test_research_returns_the_researchers_report(tmp_project: Path) -> None:
    ctx, fake = research_ctx(
        tmp_project,
        {
            "coder": [research("Which httpx version added HTTP/3?"), FakeTurn(text="Done.")],
            "researcher": [FakeTurn(text="None yet. Source: https://www.python-httpx.org/")],
        },
    )
    result = await run_agent(ctx, "find out about HTTP/3")
    assert result.stopped == "done"
    tool_result = result.messages[2].tool_result
    assert tool_result is not None and tool_result.ok
    assert tool_result.text.splitlines()[0].startswith("agent a1 (researcher) finished: done")
    assert "Source: https://www.python-httpx.org/" in tool_result.text
    request = next(r for r in fake.requests if r.model == "researcher")
    assert "You are a researcher sub-agent" in request.system
    assert request.messages[0].text() == "Which httpx version added HTTP/3?"


async def test_research_is_available_in_solo_mode_but_not_to_sub_agents(ctx: Ctx) -> None:
    ctx.state.mode = "solo"
    names = {t.name for t in agent_tools(ctx, "coder")}
    assert "research" in names and "spawn_agent" not in names
    child = replace(ctx, agent_id="a1", role="researcher")
    assert "research" not in {t.name for t in agent_tools(child, "researcher")}


async def test_deep_research_gets_more_turns(tmp_project: Path) -> None:
    look = FakeTurn(tool_calls=[FakeToolCall(name="list_dir", arguments={"path": "."})])
    ctx, _ = research_ctx(
        tmp_project,
        {"coder": [research("q"), FakeTurn(text="ok")], "researcher": [look] * 50},
    )
    await run_agent(ctx, "task")
    assert registry(ctx).agents["a1"].turns == 15

    ctx, _ = research_ctx(
        tmp_project,
        {"coder": [research("q", depth="deep"), FakeTurn(text="ok")], "researcher": [look] * 50},
    )
    await run_agent(ctx, "task")
    assert registry(ctx).agents["a1"].turns == 40


async def test_browser_research_is_refused_until_a_browser_exists(ctx: Ctx) -> None:
    result = await call_tool(
        ctx, ToolCall(id="c1", name="research", arguments={"question": "q", "browser": True})
    )
    assert not result.ok and result.code == "unsupported"


def test_coder_and_lead_prompts_prefer_research() -> None:
    for name in ("coder", "team_lead"):
        static = prompts.PROMPTS[name][0]
        assert "research tool" in static and "browser=true" in static


@respx.mock
async def test_native_search_falls_back_to_the_http_backend(
    tmp_project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def public(host: str) -> list[str]:
        return ["93.184.216.34"]

    monkeypatch.setattr(web, "resolve_host", public)
    monkeypatch.setenv("TEST_SEARCH_KEY", "k")
    respx.get("https://api.search.brave.com/res/v1/web/search").mock(
        return_value=httpx.Response(
            200, json={"web": {"results": [{"title": "T", "url": "https://t.example.com/"}]}}
        )
    )
    cfg = ForgeConfig(
        roles={"coder": ["fake/coder"]},
        web=WebConfig(fallback_backend="brave", search_api_key_env="TEST_SEARCH_KEY"),
    )
    register_provider(cfg, FakeProvider([]))  # no native search tool
    result = await call_tool(
        make_ctx(tmp_project, cfg=cfg),
        ToolCall(id="c1", name="web_search", arguments={"query": "q"}),
    )
    assert result.ok and "backend: brave" in result.text
