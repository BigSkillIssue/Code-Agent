"""The team board: read_board, claim_task, update_task (docs/TOOLS.md: Team board)."""

import asyncio
import random
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from forge.config import ForgeConfig
from forge.ctx import Ctx
from forge.local.memory_store import MemoryStore
from forge.local.sqlite_store import SqliteStore
from forge.plan import Plan, Step, TaskSpec
from forge.ports import Store
from forge.providers.base import ToolCall, ToolResult
from forge.providers.fake import FakeProvider, FakeTurn
from forge.providers.registry import register_provider
from forge.team import AgentRegistry
from forge.tools import agent_tools, call_tool
from support import make_ctx

SPEC = TaskSpec(goal="g", context="", requirements=[], acceptance_criteria=["ok"], size="large")


def step(sid: str, *deps: str, status: str = "todo") -> Step:
    return Step(
        id=sid,
        title=f"Task {sid}",
        detail="d",
        check="review: works",
        depends_on=list(deps),
        status=status,
    )  # type: ignore[arg-type]


def board_ctx(root: Path, steps: list[Step], store: Store | None = None, **cfg: Any) -> Ctx:
    ctx = make_ctx(root, cfg=ForgeConfig(**cfg), store=store or MemoryStore())
    ctx.session.plan = Plan(spec=SPEC, steps=steps)
    ctx.state.team_mode = True
    return ctx


def agent(ctx: Ctx, agent_id: str) -> Ctx:
    return replace(ctx, agent_id=agent_id, role="coder")


async def run(ctx: Ctx, name: str, **arguments: Any) -> ToolResult:
    return await call_tool(ctx, ToolCall(id="c1", name=name, arguments=arguments))


def registry(ctx: Ctx) -> AgentRegistry:
    assert isinstance(ctx.state.team, AgentRegistry)
    return ctx.state.team


async def race(ctx: Ctx) -> dict[str, list[str]]:
    """4 agents grab tasks until none is left; returns task -> agents that claimed it."""
    claims: dict[str, list[str]] = {}

    async def worker(agent_id: str) -> None:
        me = agent(ctx, agent_id)
        assert ctx.session.plan is not None
        while True:
            ready = [s for s in ctx.session.plan.steps if s.status == "todo"]
            if not ready:
                return
            target = random.choice(ready)
            result = await run(me, "claim_task", task_id=target.id)
            if result.ok:
                claims.setdefault(target.id, []).append(agent_id)
                target.status = "done"  # the work itself is not under test here
            await asyncio.sleep(0)

    await asyncio.gather(*(worker(f"a{n}") for n in range(1, 5)))
    return claims


@pytest.mark.parametrize("backend", ["memory", "sqlite"])
async def test_four_agents_never_claim_a_task_twice(
    tmp_project: Path, tmp_path: Path, backend: str
) -> None:
    store: Store = MemoryStore() if backend == "memory" else SqliteStore(tmp_path / "board.db")
    try:
        for round_ in range(50):
            ctx = board_ctx(tmp_project, [step(f"s{n}") for n in range(1, 9)], store=store)
            ctx.session.id = f"race-{round_}"
            claims = await race(ctx)
            assert sorted(claims) == [f"s{n}" for n in range(1, 9)]
            assert all(len(owners) == 1 for owners in claims.values()), claims
    finally:
        if isinstance(store, SqliteStore):
            await store.close()


async def test_blocked_task_and_second_claim_are_refused(tmp_project: Path) -> None:
    ctx = board_ctx(tmp_project, [step("s1"), step("s2"), step("s3", "s1")])
    a1 = agent(ctx, "a1")
    blocked = await run(a1, "claim_task", task_id="s3")
    assert blocked.code == "invalid_args" and "waiting for s1" in blocked.text
    claimed = await run(a1, "claim_task", task_id="s1")
    assert claimed.ok and claimed.text.splitlines()[:2] == ["claimed s1: Task s1", "detail: d"]
    second = await run(a1, "claim_task", task_id="s2")
    assert second.code == "invalid_args" and "finish s1 first" in second.text
    taken = await run(agent(ctx, "a2"), "claim_task", task_id="s1")
    assert taken.code == "busy" and "owned by a1" in taken.text
    assert (await run(a1, "claim_task", task_id="s9")).code == "not_found"


async def test_read_board_computes_ready_and_blocked(tmp_project: Path) -> None:
    ctx = board_ctx(
        tmp_project,
        [step("s1", status="done"), step("s2", "s1"), step("s3", "s1"), step("s4", "s2", "s3")],
    )
    await run(agent(ctx, "a2"), "claim_task", task_id="s2")
    board = (await run(ctx, "read_board")).text.splitlines()
    assert board[0].split() == ["id", "status", "owner", "after", "title"]
    assert board[1].split()[:3] == ["s1", "done", "-"]
    assert board[2].split()[:4] == ["s2", "doing", "a2", "s1"]
    assert board[3].split()[:2] == ["s3", "ready"]
    assert board[4].split()[:4] == ["s4", "blocked", "-", "s2,s3"]
    only_ready = (await run(ctx, "read_board", status=["ready"])).text.splitlines()
    assert [row.split()[0] for row in only_ready[1:]] == ["s3"]


def reviewer_ctx(tmp_project: Path, verdicts: list[str]) -> Ctx:
    ctx = board_ctx(tmp_project, [step("s1"), step("s2")])
    fake = FakeProvider(roles={"reviewer": [FakeTurn(text=v) for v in verdicts]})
    ctx.cfg.roles = {"reviewer": ["fake/reviewer"]}
    register_provider(ctx.cfg, fake)
    return ctx


async def test_update_task_rules(tmp_project: Path) -> None:
    ctx = reviewer_ctx(
        tmp_project, ['{"pass": false, "reason": "no tests"}', '{"pass": true, "reason": "ok"}']
    )
    a1, a2 = agent(ctx, "a1"), agent(ctx, "a2")
    await run(a1, "claim_task", task_id="s1")
    refused = await run(a2, "update_task", task_id="s1", status="done", result="did it")
    assert refused.code == "permission_denied" and "owned by a1" in refused.text
    failing = await run(a1, "update_task", task_id="s1", status="done", result="did it")
    assert failing.code == "check_failed"
    assert ctx.session.plan is not None and ctx.session.plan.steps[0].status == "doing"
    passing = await run(a1, "update_task", task_id="s1", status="done", result="did it, with tests")
    assert passing.text == "s1 done: check passed; lead notified"
    assert registry(ctx).take_messages("main") == ["[task s1 done by a1] did it, with tests"]


async def test_failed_releases_the_task_and_tells_the_lead(tmp_project: Path) -> None:
    ctx = board_ctx(tmp_project, [step("s1")])
    a1 = agent(ctx, "a1")
    await run(a1, "claim_task", task_id="s1")
    assert (
        await run(a1, "update_task", task_id="s1", status="failed", result=" ")
    ).code == "invalid_args"
    failed = await run(a1, "update_task", task_id="s1", status="failed", result="API key missing")
    assert failed.text == "s1 marked failed; lead notified"
    assert ctx.session.plan is not None and ctx.session.plan.steps[0].status == "failed"
    assert await ctx.store.owners(ctx.session.id) == {}  # type: ignore[attr-defined]
    assert registry(ctx).take_messages("main") == ["[task s1 failed by a1] API key missing"]


async def test_board_tools_only_in_team_mode(tmp_project: Path) -> None:
    ctx = board_ctx(tmp_project, [step("s1")])
    assert "claim_task" in {t.name for t in agent_tools(ctx, "coder")}
    ctx.state.team_mode = False
    assert "claim_task" not in {t.name for t in agent_tools(ctx, "coder")}
    assert (await run(ctx, "claim_task", task_id="s1")).code == "unsupported"
