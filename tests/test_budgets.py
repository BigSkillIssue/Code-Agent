"""Shared budget and mode selection (solo, sub-agents, team)."""

import json
from pathlib import Path
from typing import Any

import pytest

from forge.config import ForgeConfig, LimitsConfig
from forge.ctx import Ctx
from forge.pipeline import choose_mode, execute, report_text, run_task
from forge.plan import Plan, Step, TaskSpec
from forge.providers.base import Usage
from forge.providers.fake import FakeProvider, FakeToolCall, FakeTurn
from forge.providers.registry import register_provider
from support import make_ctx

PASS = FakeTurn(text='{"pass": true, "reason": "looks right"}')


def spec_json(size: str) -> str:
    return json.dumps(
        {
            "goal": "make three files",
            "context": "",
            "requirements": [],
            "acceptance_criteria": ["files exist"],
            "size": size,
        }
    )


def call(tool: str, **arguments: Any) -> FakeToolCall:
    return FakeToolCall(name=tool, arguments=arguments)


def pipeline_ctx(
    root: Path, roles: dict[str, list[FakeTurn]], **limits: Any
) -> tuple[Ctx, FakeProvider]:
    fake = FakeProvider(roles=roles)
    cfg = ForgeConfig(
        roles={role: [f"fake/{role}"] for role in roles}, limits=LimitsConfig(**limits)
    )
    register_provider(cfg, fake)
    return make_ctx(root, cfg=cfg, headless=True), fake


def three_steps() -> FakeTurn:
    steps = [{"title": f"step {n}", "detail": "d", "check": "review: done"} for n in (1, 2, 3)]
    return FakeTurn(tool_calls=[call("submit_plan", steps=steps)])


@pytest.mark.parametrize(
    ("size", "override", "mode"),
    [
        ("trivial", None, "solo"),
        ("small", None, "solo"),
        ("medium", None, "subagents"),
        ("large", None, "team"),
        ("large", "solo", "solo"),
        ("small", "team", "team"),
    ],
)
def test_size_picks_the_mode_and_flags_win(size: str, override: str | None, mode: str) -> None:
    spec = TaskSpec(goal="g", context="", requirements=[], acceptance_criteria=["x"], size=size)  # type: ignore[arg-type]
    assert choose_mode(spec, override) == mode


async def test_session_stops_cleanly_at_the_cost_limit(tmp_project: Path) -> None:
    costly = FakeTurn(
        text="did the step", usage=Usage(input_tokens=10, output_tokens=5, cost_usd=0.6)
    )
    ctx, fake = pipeline_ctx(
        tmp_project,
        {
            "refiner": [FakeTurn(text=spec_json("small"))],
            "planner": [three_steps(), FakeTurn(text="planned")],
            "coder": [costly, costly, costly],
            "reviewer": [PASS, PASS, PASS],
        },
        max_cost_usd=1.0,
    )
    report = await run_task("make three files", ctx)
    assert not report.ok
    assert report.summary.startswith("Stopped: the cost budget of $1.00 was used up.")
    assert "Not finished: s3 step 3." in report.summary
    assert ctx.session.plan is not None
    assert [s.status for s in ctx.session.plan.steps] == ["done", "done", "todo"]
    assert len([r for r in fake.requests if r.model == "coder"]) == 2  # no third model call
    assert "Cost: $1.2000" in report_text(report)
    assert ctx.session.status == "failed"


async def test_solo_lead_has_no_agent_tools_and_medium_gets_the_team_lead(
    tmp_project: Path,
) -> None:
    for size, expect_spawn in (("small", False), ("medium", True)):
        ctx, fake = pipeline_ctx(
            tmp_project,
            {
                "refiner": [FakeTurn(text=spec_json(size))],
                "planner": [
                    FakeTurn(
                        tool_calls=[
                            call(
                                "submit_plan",
                                steps=[{"title": "one", "detail": "d", "check": "review: ok"}],
                            )
                        ]
                    ),
                    FakeTurn(text="planned"),
                ],
                "coder": [FakeTurn(text="done")],
                "reviewer": [
                    PASS,
                    FakeTurn(text='{"ok": true, "summary": "fine", "manual_checks": []}'),
                ],
            },
        )
        await run_task("task", ctx)
        coder = next(r for r in fake.requests if r.model == "coder")
        assert ("spawn_agent" in {t.name for t in coder.tools}) is expect_spawn
        assert ("Your role: team lead." in coder.system) is expect_spawn


async def test_team_mode_workers_finish_the_board(tmp_project: Path) -> None:
    steps = [
        Step(id=f"s{n}", title=f"file {n}", detail="d", check="review: exists") for n in (1, 2)
    ]
    worker = [
        FakeTurn(tool_calls=[call("read_board")]),
        FakeTurn(tool_calls=[call("claim_task", task_id="s1")]),
        FakeTurn(tool_calls=[call("write_file", path="one.txt", content="1\n")]),
        FakeTurn(
            tool_calls=[call("update_task", task_id="s1", status="done", result="wrote one.txt")]
        ),
        FakeTurn(tool_calls=[call("claim_task", task_id="s2")]),
        FakeTurn(tool_calls=[call("write_file", path="two.txt", content="2\n")]),
        FakeTurn(
            tool_calls=[call("update_task", task_id="s2", status="done", result="wrote two.txt")]
        ),
        FakeTurn(text="completed s1 and s2"),
    ]
    ctx, _ = pipeline_ctx(
        tmp_project, {"coder": worker, "reviewer": [PASS] * 4}, max_parallel_agents=1
    )
    ctx.state.mode = "team"
    spec = TaskSpec(goal="g", context="", requirements=[], acceptance_criteria=["x"], size="large")
    plan = await execute(Plan(spec=spec, steps=steps), ctx)
    assert [s.status for s in plan.steps] == ["done", "done"]
    assert (tmp_project / "one.txt").read_text() == "1\n" and (
        tmp_project / "two.txt"
    ).read_text() == "2\n"
    assert ctx.state.team_mode
