"""The lead names each sub-agent's tools (S79), only from its own, and every agent can reach
every other agent with send_message."""

from pathlib import Path
from typing import Any

from forge.agent import run_agent
from forge.config import ForgeConfig
from forge.ctx import Ctx
from forge.providers.base import ToolResult
from forge.providers.fake import FakeProvider, FakeTurn
from forge.providers.registry import register_provider
from forge.team import AgentRegistry
from forge.tools import REGISTRY, agent_tools
from support import make_ctx
from test_browser import FakeBrowsers
from test_messaging import call, run

BROWSER = {t.name for t in REGISTRY.values() if t.group == "browser"}
TALK = {"send_message", "list_agents"}


def team(root: Path, roles: dict[str, list[FakeTurn]], browser: bool = True) -> tuple[Ctx, Any]:
    fake = FakeProvider(roles=roles)
    cfg = ForgeConfig(roles={role: [f"fake/{role}"] for role in roles})
    register_provider(cfg, fake)
    ctx = make_ctx(root, cfg=cfg)
    if browser:
        ctx.state.browser_factory = FakeBrowsers()
    return ctx, fake


def registry(ctx: Ctx) -> AgentRegistry:
    assert isinstance(ctx.state.team, AgentRegistry)
    return ctx.state.team


def offered(fake: Any, role: str) -> set[str]:
    request = next(r for r in fake.requests if r.model == role)
    return {t.name for t in request.tools}


async def test_a_sub_agent_gets_the_tools_the_lead_names(tmp_project: Path) -> None:
    spawn = call("spawn_agent", role="tester", task="click through the shop",
                 tools=["read_file", "browser"])  # fmt: skip
    ctx, fake = team(
        tmp_project,
        {
            "coder": [FakeTurn(tool_calls=[spawn]), FakeTurn(text="done")],
            "tester": [
                FakeTurn(tool_calls=[call("bash", command="echo hi")]),
                FakeTurn(text="the shop works"),
            ],
        },
    )
    await run_agent(ctx, "build the shop")
    assert offered(fake, "tester") == {"read_file"} | BROWSER | TALK
    child = registry(ctx).agents["a1"]
    assert child.tools is not None and "bash" not in child.tools
    refused = next(m.tool_result for m in child.messages if m.tool_result)
    assert not refused.ok  # bash was not given: the call is refused


async def test_the_lead_gives_only_what_it_has(tmp_project: Path) -> None:
    ctx, _ = team(tmp_project, {"coder": []}, browser=False)
    no_browser = await run(ctx, "spawn_agent", role="coder", task="x", tools=["browser"])
    assert no_browser.code == "invalid_args" and "do not have it" in no_browser.text
    unknown = await run(ctx, "spawn_agent", role="coder", task="x", tools=["teleport"])
    assert unknown.code == "invalid_args" and "no such tool or group" in unknown.text
    missing: ToolResult = await run(ctx, "spawn_agent", role="coder", task="x")
    assert missing.code == "invalid_args"  # the lead must always name the tools
    assert registry(ctx).agents.keys() == {"main"}


def test_a_read_only_role_keeps_only_read_only_tools(tmp_project: Path) -> None:
    ctx = make_ctx(tmp_project, agent_id="a1", role="reviewer",
                   granted_tools=frozenset({"files", "shell"}))  # fmt: skip
    names = {t.name for t in agent_tools(ctx, "reviewer")}
    assert "read_file" in names and "write_file" not in names and "bash" not in names
    assert names >= TALK


def test_every_sub_agent_can_talk_to_the_others(tmp_project: Path) -> None:
    lead = make_ctx(tmp_project)
    for role, granted in (("browser", None), ("coder", frozenset()), ("explore", None)):
        sub = make_ctx(tmp_project, agent_id="a1", role=role, granted_tools=granted)
        sub.state = lead.state
        assert {t.name for t in agent_tools(sub, role)} >= TALK, role


async def test_siblings_message_each_other_by_name(tmp_project: Path) -> None:
    lead = make_ctx(tmp_project)
    agents = registry(lead)
    api = agents.add("tester", "build the orders API", "api", "main")
    web = agents.add("explore", "build the orders page", "web", "main")
    api_ctx = make_ctx(tmp_project, agent_id=api.id, role="tester", granted_tools=frozenset())
    api_ctx.state = lead.state
    sent = await run(api_ctx, "send_message", to="web", text="GET /orders returns {id, total}")
    assert sent.ok and f"delivered to {web.id}" in sent.text
    assert agents.take_messages(web.id) == [
        f"[message from {api.id} (tester)]\n> GET /orders returns {{id, total}}"
    ]
    back = make_ctx(tmp_project, agent_id=web.id, role="explore")
    back.state = lead.state
    assert (await run(back, "send_message", to="main", text="the page is ready")).ok
    assert agents.take_messages("main") == [
        f"[message from {web.id} (explore)]\n> the page is ready"
    ]
