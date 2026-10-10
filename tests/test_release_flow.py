"""Checkpoints as plug-ins (S67a): the pipeline calls each checkpoint at the request, the plan
and the product, in order; a checkpoint can stop the run, change the plan and set Report fields.
The Apple checkpoint is the first one (its own tests stay in test_apple_pipeline.py)."""

from pathlib import Path
from typing import Any

import pytest

from forge import pipeline
from forge.apple_flow import AppleCheckpoint, AppleStopped
from forge.config import AppleConfig, ForgeConfig
from forge.ctx import Ctx
from forge.pipeline import checkpoints_for, run_task
from forge.plan import Plan, TaskSpec
from forge.providers.fake import FakeProvider, FakeToolCall, FakeTurn
from forge.release_flow import CheckpointStopped, Outcome, Replan
from forge.wiring import install_fake
from support import make_ctx

SPEC = TaskSpec(
    goal="A notes app",
    context="",
    requirements=["notes can be written"],
    acceptance_criteria=["a note is saved"],
    size="small",
)
STEPS = [{"title": "Notes", "detail": "a list of notes", "check": "review: done"}]


class Dummy:
    """A checkpoint that records when it is called and can stop or change the run."""

    def __init__(self, name: str, log: list[str], stop_at: str = "", constraint: str = "") -> None:
        self.name = name
        self.log = log
        self.stop_at = stop_at
        self.constraint = constraint

    def at(self, point: str) -> None:
        self.log.append(f"{self.name}:{point}")
        if point == self.stop_at:
            raise CheckpointStopped(f"{self.name} stopped the run at the {point}")

    async def check_request(self, ctx: Ctx, prompt: str) -> None:
        self.at("request")

    async def check_plan(self, ctx: Ctx, plan: Plan, replan: Replan) -> Plan:
        self.at("plan")
        if not self.constraint:
            return plan
        spec = plan.spec.model_copy(update={"constraints": [self.constraint]})
        return await replan(spec)

    async def finish(self, ctx: Ctx) -> Outcome:
        self.at("product")
        return Outcome(ready=True, summary=f"{self.name} approved")

    def report_fields(self, outcome: Outcome) -> dict[str, Any]:
        return {"ready_for_apple": outcome.ready, "apple_summary": outcome.summary}


def run_ctx(root: Path, plans: int = 1, spec: TaskSpec = SPEC) -> tuple[Ctx, FakeProvider]:
    """A context whose models refine, plan (`plans` times), code and review."""
    call = FakeToolCall(name="submit_plan", arguments={"steps": STEPS})
    planner = [
        turn
        for _ in range(plans)
        for turn in (FakeTurn(tool_calls=[call]), FakeTurn(text="planned"))
    ]
    write = FakeToolCall(name="write_file", arguments={"path": "notes.py", "content": "x = 1\n"})
    roles: dict[str, list[Any]] = {
        "refiner": [FakeTurn(text=spec.model_dump_json())],
        "planner": planner,
        "coder": [FakeTurn(tool_calls=[write]), FakeTurn(text="written")],
        "reviewer": [
            FakeTurn(text='{"pass": true, "reason": "ok"}'),
            FakeTurn(text='{"ok": true, "summary": "Notes added."}'),
        ],
    }
    ctx = make_ctx(root)
    fake = install_fake(ctx.cfg, FakeProvider(roles=roles))
    return ctx, fake


def use(monkeypatch: pytest.MonkeyPatch, *checkpoints: Dummy) -> None:
    """Run these checkpoints instead of the configured ones."""
    monkeypatch.setattr(pipeline, "checkpoints_for", lambda ctx: list(checkpoints))


async def test_a_checkpoint_is_called_at_the_three_points_in_order(
    tmp_project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    log: list[str] = []
    use(monkeypatch, Dummy("one", log))
    ctx, fake = run_ctx(tmp_project)
    report = await run_task("a notes app", ctx)
    assert report.ok and report.ready_for_apple and report.apple_summary == "one approved"
    assert log == ["one:request", "one:plan", "one:product"]
    roles = [r.model for r in fake.requests]
    assert roles.index("refiner") < roles.index("planner") < roles.index("coder")


async def test_two_checkpoints_run_one_after_the_other(
    tmp_project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    log: list[str] = []
    use(monkeypatch, Dummy("one", log), Dummy("two", log))
    ctx, _ = run_ctx(tmp_project)
    await run_task("a notes app", ctx)
    assert log == [
        "one:request",
        "two:request",
        "one:plan",
        "two:plan",
        "one:product",
        "two:product",
    ]


async def test_a_checkpoint_can_stop_the_run_before_planning(
    tmp_project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    log: list[str] = []
    use(monkeypatch, Dummy("one", log, stop_at="request"), Dummy("two", log))
    ctx, fake = run_ctx(tmp_project)
    report = await run_task("a notes app", ctx)
    assert not report.ok and report.summary == "one stopped the run at the request"
    assert log == ["one:request"]
    assert fake.requests == []


async def test_a_checkpoint_can_have_the_plan_made_again(
    tmp_project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    log: list[str] = []
    use(monkeypatch, Dummy("one", log, constraint="notes are private"))
    ctx, fake = run_ctx(tmp_project, plans=2)
    report = await run_task("a notes app", ctx)
    assert report.ok
    planner = [r for r in fake.requests if r.model == "planner"]
    assert len(planner) >= 2
    assert "notes are private" in planner[-1].system  # the plan was made again with it


async def test_a_trivial_task_with_a_checkpoint_is_still_planned(
    tmp_project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    log: list[str] = []
    use(monkeypatch, Dummy("one", log))
    ctx, fake = run_ctx(tmp_project, spec=SPEC.model_copy(update={"size": "trivial"}))
    await run_task("fix a typo", ctx)
    assert log == ["one:request", "one:plan", "one:product"]
    assert any(r.model == "planner" for r in fake.requests)


def test_apple_is_the_first_checkpoint(tmp_project: Path) -> None:
    assert checkpoints_for(make_ctx(tmp_project)) == []
    cfg = ForgeConfig(apple=AppleConfig(review=True))
    found = checkpoints_for(make_ctx(tmp_project, cfg=cfg))
    assert [type(c) for c in found] == [AppleCheckpoint]
    assert issubclass(AppleStopped, CheckpointStopped)
    fields = found[0].report_fields(Outcome(ready=True, summary="approved"))
    assert fields == {"ready_for_apple": True, "apple_summary": "approved"}
