"""Tests for ask_user and the clarify stage."""

import json
from pathlib import Path
from typing import Any

from forge.config import ForgeConfig, LimitsConfig
from forge.ctx import Ctx
from forge.pipeline import clarify, refine
from forge.plan import Question, TaskSpec
from forge.ports import Answer
from forge.providers.base import ToolCall, ToolResult
from forge.providers.fake import FakeProvider, FakeTurn
from forge.providers.registry import register_provider
from forge.tools import call_tool
from support import ScriptedRenderer, make_ctx

DB = Question(
    text="Which database should the cache use?",
    kind="choice",
    options=["Redis", "Memcached"],
    default="Redis",
    why="dependency",
)
TTL = Question(
    text="Should cached entries expire?", kind="confirm", default="yes", why="memory use"
)


def spec(*questions: Question) -> TaskSpec:
    return TaskSpec(
        goal="Add a cache",
        context="no cache yet",
        requirements=["cache responses"],
        acceptance_criteria=["pytest"],
        open_questions=list(questions),
        size="medium",
    )


def ctx_with(
    root: Path,
    renderer: ScriptedRenderer | None = None,
    *turns: FakeTurn,
    headless: bool = False,
    rounds: int = 3,
) -> tuple[Ctx, FakeProvider]:
    fake = FakeProvider(roles={"refiner": list(turns)})
    cfg = ForgeConfig(
        roles={"refiner": ["fake/refiner"]}, limits=LimitsConfig(max_clarify_rounds=rounds)
    )
    register_provider(cfg, fake)
    return make_ctx(root, cfg=cfg, renderer=renderer, headless=headless), fake


def answers(*values: str) -> list[Answer]:
    return [Answer(question_index=i, values=[v]) for i, v in enumerate(values)]


async def test_answers_end_up_in_the_spec(tmp_project: Path) -> None:
    merged = spec().model_copy(update={"goal": "Add a Redis cache with expiry"})
    renderer = ScriptedRenderer(answers=[answers("Redis", "yes")])
    ctx, fake = ctx_with(tmp_project, renderer, FakeTurn(text=merged.model_dump_json()))
    result = await clarify(spec(DB, TTL), ctx)
    assert result.goal == "Add a Redis cache with expiry"
    assert result.open_questions == []
    sent = fake.requests[0].messages[-1].text()
    assert "Which database should the cache use? -> Redis" in sent


async def test_headless_records_assumptions(tmp_project: Path) -> None:
    ctx, fake = ctx_with(tmp_project, headless=True)
    result = await clarify(spec(DB, TTL), ctx)
    assert result.assumptions == [
        'Assumed for "Which database should the cache use?": Redis',
        'Assumed for "Should cached entries expire?": yes',
    ]
    assert fake.requests == []


async def test_round_limit_is_respected(tmp_project: Path) -> None:
    still_open = spec(TTL).model_dump_json()
    renderer = ScriptedRenderer(answers=[answers("Redis"), answers("no")])
    ctx, _ = ctx_with(
        tmp_project, renderer, FakeTurn(text=still_open), FakeTurn(text=still_open), rounds=2
    )
    result = await clarify(spec(DB), ctx)
    assert len(renderer.questions) == 2
    assert result.open_questions == [] and any("expire" in a for a in result.assumptions)


async def test_go_stops_asking(tmp_project: Path) -> None:
    renderer = ScriptedRenderer(answers=[answers("/go", "no")])
    ctx, fake = ctx_with(tmp_project, renderer)
    result = await clarify(spec(DB, TTL), ctx)
    assert fake.requests == []
    assert 'Assumed for "Which database should the cache use?": Redis' in result.assumptions


async def test_no_question_when_the_repo_has_the_answer(tmp_project: Path) -> None:
    (tmp_project / "README.md").write_text("The cache uses Redis (see docker-compose.yml).\n")
    answered = spec().model_copy(update={"context": "README says the cache uses Redis"})
    renderer = ScriptedRenderer()
    ctx, fake = ctx_with(tmp_project, renderer, FakeTurn(text=answered.model_dump_json()))
    result = await clarify(await refine("Add a cache", ctx), ctx)
    assert "README.md" in fake.requests[0].system
    assert renderer.questions == [] and result.open_questions == []


async def run_ask(ctx: Ctx, **arguments: Any) -> ToolResult:
    return await call_tool(ctx, ToolCall(id="c1", name="ask_user", arguments=arguments))


def q(**fields: Any) -> dict[str, Any]:
    return {
        "text": "Pick one?",
        "kind": "choice",
        "options": ["a", "b"],
        "why": "matters",
        **fields,
    }


async def test_ask_user_returns_answers_in_order(tmp_project: Path) -> None:
    renderer = ScriptedRenderer(answers=[answers("b", "free text")])
    ctx, _ = ctx_with(tmp_project, renderer)
    result = await run_ask(ctx, questions=[q(), q(text="Name?", kind="text", options=[])])
    assert result.text == "1. Pick one?\n   answer: b\n2. Name?\n   answer: free text"


async def test_ask_user_headless_uses_defaults(tmp_project: Path) -> None:
    ctx, _ = ctx_with(tmp_project, headless=True)
    result = await run_ask(ctx, questions=[q(default="b")])
    assert result.text.endswith("answer: b (default, headless)")
    assert ctx.state.notes == ['Assumed for "Pick one?": b']


async def test_ask_user_refuses_bad_input_and_sub_agents(tmp_project: Path) -> None:
    ctx, _ = ctx_with(tmp_project)
    assert (await run_ask(ctx, questions=[q()] * 5)).code == "invalid_args"
    assert (
        "default must be one of the options"
        in (await run_ask(ctx, questions=[q(default="c")])).text
    )
    ctx.agent_id = "a1"
    assert (await run_ask(ctx, questions=[q()])).code == "unsupported"


async def test_dismissed_questions_are_cancelled(tmp_project: Path) -> None:
    renderer = ScriptedRenderer(answers=[[]])
    ctx, _ = ctx_with(tmp_project, renderer)
    result = await run_ask(ctx, questions=[q()])
    assert result.code == "cancelled" and "best judgment" in result.text
    assert json.loads(json.dumps(result.model_dump()))["ok"] is False
