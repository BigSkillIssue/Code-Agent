"""A team of agents per area that talk to each other (S77)."""

import asyncio
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

from forge import prompts
from forge.blueprint import Blueprint, Component, planned_spec
from forge.board import plan_areas
from forge.config import ForgeConfig
from forge.ctx import Ctx
from forge.pipeline import mode_for_plan
from forge.plan import Plan, Step, TaskSpec
from forge.providers.base import ChatRequest
from forge.providers.fake import FakeProvider, FakeToolCall, FakeTurn
from forge.providers.registry import register_provider
from forge.team import AgentRegistry
from support import make_ctx

SPEC = TaskSpec(goal="g", context="", requirements=[], acceptance_criteria=["x"], size="medium")
FULL = dict.fromkeys(prompts.KNOWN_SLOTS, "")


def call(tool: str, **arguments: Any) -> FakeToolCall:
    return FakeToolCall(name=tool, arguments=arguments)


def plan_of(*titles: str) -> Plan:
    steps = [
        Step(id=f"s{n}", title=t, detail="d", check="review: ok") for n, t in enumerate(titles, 1)
    ]
    return Plan(spec=SPEC, steps=steps)


def registry(ctx: Ctx) -> AgentRegistry:
    assert isinstance(ctx.state.team, AgentRegistry)
    return ctx.state.team


def test_the_prompts_push_towards_one_agent_per_area_that_talk() -> None:
    lead = prompts.render("team_lead", **FULL)
    assert "one named background agent per area" in lead
    assert 'name="database"' in lead and 'name="api"' in lead
    assert "docs/architecture.md" in lead and "send_message" in lead
    member = prompts.render("team_member", **FULL)
    assert "list_agents" in member and "send_message" in member
    assert "docs/architecture.md" in member
    planner = prompts.render("planner", **FULL)
    assert "database: add the orders table" in planner
    area = prompts.render("team_area_task", material="Your area: api.")
    assert "send_message" in area and "docs/architecture.md" in area


def test_areas_come_from_the_step_titles_and_a_plan_with_several_gets_a_team() -> None:
    areas = plan_of("Database: add the orders table", "api: add GET /orders", "web: list orders",
                    "api: add POST /orders")  # fmt: skip
    assert plan_areas(areas) == ["database", "api", "web"]
    assert plan_areas(plan_of("api: one", "api: two")) == []  # one area needs no team
    assert plan_areas(plan_of("Fix the typo", "Run the tests")) == []
    assert mode_for_plan(areas, "subagents", None) == "team"
    assert mode_for_plan(areas, "subagents", "subagents") == "subagents"  # the user chose
    assert mode_for_plan(areas, "solo", None) == "solo"
    assert mode_for_plan(plan_of("one", "two"), "subagents", None) == "subagents"


def test_a_blueprint_asks_for_its_components_as_areas() -> None:
    blueprint = Blueprint.model_construct(
        components=[Component(name="server", kind="server", responsibilities=["API"]),
                    Component(name="web", kind="web", responsibilities=["UI"])]
    )  # fmt: skip
    [constraint] = planned_spec(SPEC, blueprint, []).constraints
    assert "server: " in constraint and "and a colon" in constraint


def area_turns(ctx: Ctx, scripts: dict[str, list[FakeTurn]]) -> Callable[[ChatRequest], FakeTurn]:
    """Each worker's next turn, by the area named in its task."""

    def turn(request: ChatRequest) -> FakeTurn:
        found = re.search(r"Your area: ([a-z]+)", request.messages[0].text())
        assert found is not None
        area, wait = found.group(1), FakeTurn(tool_calls=[call("list_dir")])
        if area == "database" and registry(ctx).find("api") is None:
            return wait  # until the api agent exists
        if area == "api" and not any("orders: id, total" in m.text() for m in request.messages):
            return wait  # until the database agent's message is there
        return scripts[area].pop(0)

    return turn


async def test_team_workers_are_named_after_the_areas_and_message_each_other(
    tmp_project: Path,
) -> None:
    scripts = {
        "database": [
            FakeTurn(tool_calls=[call("send_message", to="api", text="orders: id, total")]),
            FakeTurn(text="database done"),
        ],
        "api": [FakeTurn(text="api done")],
    }
    ctx = make_ctx(tmp_project, cfg=ForgeConfig(roles={"coder": ["fake/coder"]}))
    fake = FakeProvider(roles={"coder": [area_turns(ctx, scripts)] * 20})
    register_provider(ctx.cfg, fake)
    ctx.state.mode = "team"
    plan = plan_of("database: add the orders table", "api: add GET /orders")
    ctx.session.plan = plan
    await asyncio.wait_for(registry(ctx).run_team(ctx, plan), 20)
    agents = [a for a in registry(ctx).agents.values() if a.id != "main"]
    assert sorted((a.name or "", a.status) for a in agents) == [
        ("api", "done"),
        ("database", "done"),
    ]
    first = next(r for r in fake.requests if "Your area: database" in r.messages[0].text())
    assert "The other areas, each with its own agent of that name: api." in first.messages[0].text()
    api = [r for r in fake.requests if "Your area: api" in r.messages[0].text()]
    assert any("orders: id, total" in m.text() for r in api for m in r.messages)
