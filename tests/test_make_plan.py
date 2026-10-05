"""Tests for submit_plan and the plan stage."""

from pathlib import Path
from typing import Any

import pytest

from forge.config import ForgeConfig
from forge.ctx import Ctx
from forge.events import PlanUpdated
from forge.pipeline import PipelineError, PlanRejected, make_plan
from forge.plan import TaskSpec
from forge.ports import Approval
from forge.providers.base import ToolCall
from forge.providers.fake import FakeProvider, FakeToolCall, FakeTurn
from forge.providers.registry import register_provider
from forge.tools import call_tool
from support import ScriptedRenderer, drain, make_ctx

SPEC = TaskSpec(
    goal="Add JWT auth",
    context="FastAPI app",
    requirements=["tokens"],
    acceptance_criteria=["pytest"],
    size="medium",
)


def step(
    title: str,
    *deps: int,
    check: str = "pytest -q",
    role: str = "coder",
    files: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "title": title,
        "detail": f"do {title}",
        "files": files or [],
        "depends_on": list(deps),
        "check": check,
        "role": role,
    }


GOOD = [
    step("Add Token model"),
    step("Add JWT middleware", 1),
    step("Add login tests", 2, check="review: covers expired tokens", role="tester"),
]


def submit(steps: list[dict[str, Any]]) -> FakeTurn:
    return FakeTurn(
        tool_calls=[
            FakeToolCall(
                name="submit_plan", arguments={"steps": steps, "explanation": "small steps"}
            )
        ]
    )


def planner_ctx(
    root: Path, *turns: FakeTurn, renderer: ScriptedRenderer | None = None, headless: bool = False
) -> tuple[Ctx, FakeProvider]:
    fake = FakeProvider(roles={"planner": list(turns)})
    cfg = ForgeConfig(roles={"planner": ["fake/planner"], "coder": ["fake/coder"]})
    register_provider(cfg, fake)
    return make_ctx(root, cfg=cfg, renderer=renderer, headless=headless), fake


async def test_spec_becomes_a_valid_plan(tmp_project: Path) -> None:
    ctx, fake = planner_ctx(
        tmp_project,
        FakeTurn(tool_calls=[FakeToolCall(name="list_dir", arguments={})]),
        submit(GOOD),
        FakeTurn(text="Plan submitted."),
        headless=True,
    )
    events = ctx.bus.subscribe("*")
    plan = await make_plan(SPEC, ctx)
    assert [s.id for s in plan.steps] == ["s1", "s2", "s3"]
    assert plan.steps[2].depends_on == ["s2"] and plan.steps[2].role == "tester"
    assert plan.validate_graph() == [] and 3 <= len(plan.steps) <= 12
    assert any(isinstance(e, PlanUpdated) for e in await drain(events))
    assert "Add JWT auth" in fake.requests[0].system  # the spec reaches the planner
    tool_names = {t.name for t in fake.requests[0].tools}
    assert "submit_plan" in tool_names and "edit_file" not in tool_names
    saved = await ctx.store.load_session(ctx.session.id)
    assert saved.plan is not None and saved.plan.version == 1


async def test_invalid_plan_is_rejected_and_fixed(tmp_project: Path) -> None:
    bad = [step("A", 2), step("B", 1), step("C", role="wizard")]
    ctx, fake = planner_ctx(
        tmp_project, submit(bad), submit(GOOD), FakeTurn(text="ok"), headless=True
    )
    plan = await make_plan(SPEC, ctx)
    assert len(plan.steps) == 3
    feedback = fake.requests[1].messages[-1].tool_result
    assert feedback is not None and feedback.code == "invalid_args"
    assert "dependency cycle" in feedback.text and "unknown role 'wizard'" in feedback.text


async def test_user_feedback_leads_to_a_revision(tmp_project: Path) -> None:
    renderer = ScriptedRenderer(
        approvals=[Approval(allow=False, feedback="merge steps 1 and 2"), Approval(allow=True)]
    )
    ctx, fake = planner_ctx(
        tmp_project,
        submit(GOOD),
        submit([step("Add token model and middleware"), step("Add login tests", 1)]),
        FakeTurn(text="ok"),
        renderer=renderer,
    )
    plan = await make_plan(SPEC, ctx)
    assert len(plan.steps) == 2
    assert "merge steps 1 and 2" in (fake.requests[1].messages[-1].tool_result.text)  # type: ignore[union-attr]
    assert "approve this plan?" in renderer.approval_requests[0][1]


async def test_rejection_ends_cleanly(tmp_project: Path) -> None:
    renderer = ScriptedRenderer(approvals=[Approval(allow=False)])
    ctx, _ = planner_ctx(
        tmp_project, submit(GOOD), FakeTurn(text="Understood, stopping."), renderer=renderer
    )
    with pytest.raises(PlanRejected):
        await make_plan(SPEC, ctx)
    assert ctx.session.plan is None


async def test_no_plan_submitted(tmp_project: Path) -> None:
    ctx, _ = planner_ctx(tmp_project, FakeTurn(text="I cannot plan this."), headless=True)
    with pytest.raises(PipelineError, match="did not submit"):
        await make_plan(SPEC, ctx)


async def test_resubmission_bumps_the_version_and_files_must_be_inside(tmp_project: Path) -> None:
    ctx, _ = planner_ctx(tmp_project, submit(GOOD), FakeTurn(text="ok"), headless=True)
    await make_plan(SPEC, ctx)
    ctx.role = "planner"
    again = await call_tool(ctx, ToolCall(id="c", name="submit_plan", arguments={"steps": GOOD}))
    assert "version 2" in again.text
    outside = await call_tool(
        ctx,
        ToolCall(
            id="c", name="submit_plan", arguments={"steps": [step("X", files=["../elsewhere.py"])]}
        ),
    )
    assert outside.code == "invalid_args" and "outside the project" in outside.text


async def test_only_planners_may_submit(tmp_project: Path) -> None:
    ctx, _ = planner_ctx(tmp_project)
    ctx.session.spec = SPEC
    result = await call_tool(ctx, ToolCall(id="c", name="submit_plan", arguments={"steps": GOOD}))
    assert result.code == "unsupported"
