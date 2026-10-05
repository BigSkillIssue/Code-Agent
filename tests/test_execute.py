"""Tests for executing a plan: finish_step, verification, retries, replanning."""

import sys
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from forge.config import ForgeConfig
from forge.ctx import Ctx
from forge.local.local_executor import LocalExecutor
from forge.pipeline import execute
from forge.plan import Plan, Step, TaskSpec
from forge.providers.base import ToolCall
from forge.providers.fake import FakeProvider, FakeToolCall, FakeTurn
from forge.providers.registry import register_provider
from forge.tools import call_tool
from support import make_ctx

PY = Path(sys.executable).as_posix()
SPEC = TaskSpec(
    goal="g", context="c", requirements=[], acceptance_criteria=["files exist"], size="small"
)


def exists_check(name: str) -> str:
    """A check command that passes when `name` exists, in the platform's check shell."""
    code = f"import pathlib, sys; sys.exit(0 if pathlib.Path('{name}').exists() else 1)"
    prefix = "& " if sys.platform == "win32" else ""
    return f'{prefix}"{PY}" -c "{code}"'


def plan_of(*names: str) -> Plan:
    steps = [
        Step(
            id=f"s{i}",
            title=f"create {n}",
            detail=f"create the file {n}",
            check=exists_check(n),
            depends_on=[f"s{i - 1}"] if i > 1 else [],
        )
        for i, n in enumerate(names, start=1)
    ]
    return Plan(spec=SPEC, steps=steps)


def calls(*items: tuple[str, dict[str, Any]]) -> FakeTurn:
    return FakeTurn(tool_calls=[FakeToolCall(name=n, arguments=a) for n, a in items])


def write(name: str) -> tuple[str, dict[str, Any]]:
    return ("write_file", {"path": name, "content": "x\n"})


def finish(step_id: str) -> tuple[str, dict[str, Any]]:
    return ("finish_step", {"step_id": step_id, "summary": f"did {step_id}"})


OPEN: list[LocalExecutor] = []


@pytest.fixture
async def project(tmp_project: Path) -> AsyncIterator[Path]:
    yield tmp_project
    while OPEN:
        await OPEN.pop().close()


async def make(root: Path, coder: list[FakeTurn], replanner: list[FakeTurn] | None = None) -> Ctx:
    fake = FakeProvider(roles={"coder": coder, "replanner": replanner or []})
    cfg = ForgeConfig(roles={"coder": ["fake/coder"], "replanner": ["fake/replanner"]})
    register_provider(cfg, fake)
    executor = LocalExecutor(root.resolve())
    OPEN.append(executor)
    ctx = make_ctx(root, cfg=cfg, executor=executor, headless=True)
    ctx.session.spec = SPEC
    return ctx


async def test_all_steps_pass(project: Path) -> None:
    ctx = await make(
        project,
        [
            calls(write("a.txt")),
            calls(finish("s1")),
            FakeTurn(text="s1 done"),
            calls(write("b.txt"), finish("s2")),
            FakeTurn(text="s2 done"),
        ],
    )
    plan = await execute(plan_of("a.txt", "b.txt"), ctx)
    assert [s.status for s in plan.steps] == ["done", "done"]
    assert plan.steps[0].notes == "did s1"
    saved = await ctx.store.load_session(ctx.session.id)
    assert saved.plan is not None and saved.plan.steps[1].status == "done"


async def test_step_fails_once_then_passes(project: Path) -> None:
    ctx = await make(
        project, [calls(finish("s1")), calls(write("a.txt"), finish("s1")), FakeTurn(text="fixed")]
    )
    plan = await execute(plan_of("a.txt"), ctx)
    assert plan.steps[0].status == "done" and plan.steps[0].attempts == 1


async def test_check_failure_reaches_the_model(project: Path) -> None:
    fake_turns = [calls(finish("s1")), calls(write("a.txt"), finish("s1")), FakeTurn(text="ok")]
    ctx = await make(project, fake_turns)
    await execute(plan_of("a.txt"), ctx)
    fake = ctx.cfg.instances["fake"]
    result = fake.requests[1].messages[-1].tool_result
    assert result.code == "check_failed" and "attempt 1 of 3" in result.text


async def test_step_failing_three_times_is_replanned(project: Path) -> None:
    coder = [
        calls(finish("s1")),
        calls(finish("s1")),
        calls(finish("s1")),
        FakeTurn(text="giving up"),
        calls(write("c.txt"), finish("s2")),
        FakeTurn(text="done"),
    ]
    new_plan = [{"title": "create c.txt instead", "detail": "", "check": exists_check("c.txt")}]
    replanner = [
        FakeTurn(tool_calls=[FakeToolCall(name="submit_plan", arguments={"steps": new_plan})]),
        FakeTurn(text="replanned"),
    ]
    ctx = await make(project, coder, replanner)
    plan = await execute(plan_of("a.txt"), ctx)
    assert plan.version == 2
    assert [(s.id, s.status) for s in plan.steps] == [("s1", "done")] or [
        s.title for s in plan.steps
    ] == ["create c.txt instead"]
    assert plan.steps[-1].status == "done" and (project / "c.txt").exists()


async def test_agent_stopping_without_finish_is_checked(project: Path) -> None:
    ctx = await make(project, [calls(write("a.txt")), FakeTurn(text="I wrote it.")])
    plan = await execute(plan_of("a.txt"), ctx)
    assert plan.steps[0].status == "done" and plan.steps[0].notes == "I wrote it."


async def run(ctx: Ctx, name: str, **arguments: Any) -> str:
    result = await call_tool(ctx, ToolCall(id="c", name=name, arguments=arguments))
    return f"{result.code}|{result.text}"


async def test_model_cannot_mark_done_without_a_passing_check(project: Path) -> None:
    ctx = await make(project, [])
    ctx.session.plan = plan_of("a.txt", "b.txt")
    out = await run(ctx, "update_plan", updates=[{"step_id": "s1", "status": "done"}])
    assert out.startswith("invalid_args|") and "use finish_step" in out
    await run(ctx, "update_plan", updates=[{"step_id": "s1", "status": "doing"}])
    out = await run(ctx, "finish_step", step_id="s1", summary="trust me")
    assert out.startswith("check_failed|")
    assert ctx.session.plan.steps[0].status == "doing"
    assert (await run(ctx, "finish_step", step_id="s2", summary="x")).startswith("invalid_args|")


async def test_update_plan_transitions(project: Path) -> None:
    ctx = await make(project, [])
    ctx.session.plan = plan_of("a.txt", "b.txt")
    assert "skipping s1 needs a note" in await run(
        ctx, "update_plan", updates=[{"step_id": "s1", "status": "skipped"}]
    )
    both = [{"step_id": "s1", "status": "doing"}, {"step_id": "s2", "status": "doing"}]
    assert "at most one step can be doing" in await run(ctx, "update_plan", updates=both)
    assert ctx.session.plan.steps[0].status == "todo"  # a failed batch changes nothing
    out = await run(
        ctx,
        "update_plan",
        updates=[{"step_id": "s1", "status": "skipped", "note": "no docs folder"}],
    )
    assert out.endswith(
        "[-] s1 create a.txt (skipped: no docs folder)\n[ ] s2 create b.txt (after s1)"
    )
    assert "cannot go from skipped to doing" in await run(
        ctx, "update_plan", updates=[{"step_id": "s1", "status": "doing"}]
    )
