"""Phase 1 gate: a multi-step plan survives being killed and resumes."""

import asyncio
import sys
from pathlib import Path

from forge.config import ForgeConfig
from forge.ctx import Ctx
from forge.local.local_executor import LocalExecutor
from forge.local.sqlite_store import SqliteStore
from forge.pipeline import execute, resume
from forge.plan import Plan, Step, TaskSpec
from forge.providers.fake import FakeProvider, FakeToolCall, FakeTurn
from forge.providers.registry import register_provider
from support import make_ctx

PY = Path(sys.executable).as_posix()
SPEC = TaskSpec(
    goal="create files",
    context="",
    requirements=[],
    acceptance_criteria=["they exist"],
    size="small",
)


def check(name: str) -> str:
    code = f"import pathlib, sys; sys.exit(0 if pathlib.Path('{name}').exists() else 1)"
    return ("& " if sys.platform == "win32" else "") + f'"{PY}" -c "{code}"'


def plan() -> Plan:
    names = ["a.txt", "b.txt", "c.txt"]
    steps = [
        Step(id=f"s{i}", title=f"create {n}", detail="", check=check(n))
        for i, n in enumerate(names, 1)
    ]
    return Plan(spec=SPEC, steps=steps)


def turn(*calls: tuple[str, dict[str, object]], text: str = "") -> FakeTurn:
    return FakeTurn(
        text=text, tool_calls=[FakeToolCall(name=n, arguments=dict(a)) for n, a in calls]
    )


def ctx_for(root: Path, store: SqliteStore, turns: list[FakeTurn], session: object = None) -> Ctx:
    cfg = ForgeConfig(roles={"coder": ["fake/coder"]})
    register_provider(cfg, FakeProvider(turns))
    ctx = make_ctx(root, cfg=cfg, executor=LocalExecutor(root.resolve()), headless=True)
    ctx.store = store
    return ctx


async def test_kill_mid_plan_then_resume(tmp_project: Path, tmp_path: Path) -> None:
    db = tmp_path / "forge.db"
    store = SqliteStore(db)
    first_run = [
        turn(
            ("write_file", {"path": "a.txt", "content": "a"}),
            ("finish_step", {"step_id": "s1", "summary": "a"}),
        ),
        turn(text="s1 done"),
        turn(("bash", {"command": "sleep 30"})),  # step 2 hangs: the process is "killed" here
    ]
    ctx = ctx_for(tmp_project, store, first_run)
    session = await store.create_session(str(tmp_project.resolve()))
    ctx.session = session
    ctx.session.spec = SPEC
    task = asyncio.create_task(execute(plan(), ctx))
    for _ in range(200):
        saved = await store.load_session(session.id)
        if saved.plan and saved.plan.steps[1].status == "doing":
            break
        await asyncio.sleep(0.05)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    await ctx.executor.close()  # type: ignore[attr-defined]
    await store.close()

    store = SqliteStore(db)  # a fresh process: only the database survives
    saved = await store.load_session(session.id)
    assert saved.plan is not None and [s.status for s in saved.plan.steps] == [
        "done",
        "doing",
        "todo",
    ]
    second_run = [
        turn(
            ("write_file", {"path": "b.txt", "content": "b"}),
            ("finish_step", {"step_id": "s2", "summary": "b"}),
        ),
        turn(text="s2 done"),
        turn(
            ("write_file", {"path": "c.txt", "content": "c"}),
            ("finish_step", {"step_id": "s3", "summary": "c"}),
        ),
        turn(text="s3 done"),
    ]
    ctx = ctx_for(tmp_project, store, second_run)
    ctx.session = saved
    final = await resume(ctx)
    assert [s.status for s in final.steps] == ["done", "done", "done"]
    assert (await store.load_session(session.id)).plan.steps[2].status == "done"  # type: ignore[union-attr]
    await ctx.executor.close()  # type: ignore[attr-defined]
    await store.close()
