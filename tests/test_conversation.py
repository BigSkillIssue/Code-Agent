"""One ongoing conversation per session that the agent keeps tidy itself (S76)."""

import json
from pathlib import Path

from forge import prompts
from forge.agent import run_agent
from forge.config import ForgeConfig
from forge.conversation import (
    KEEP_TURNS,
    MAX_TURN_CHARS,
    RESULT_CHARS,
    conversation_text,
    record_turn,
)
from forge.ctx import Ctx
from forge.events import Compacted
from forge.local.sqlite_store import SqliteStore
from forge.pipeline import run_task
from forge.ports import Turn
from forge.providers.fake import FakeProvider, FakeToolCall, FakeTurn
from forge.providers.registry import register_provider
from support import drain, make_ctx


def trivial(goal: str) -> FakeTurn:
    spec = {"goal": goal, "context": "", "requirements": [], "acceptance_criteria": ["done"],
            "size": "trivial"}  # fmt: skip
    return FakeTurn(text=json.dumps(spec))


def conversation_ctx(root: Path, roles: dict[str, list[FakeTurn]]) -> tuple[Ctx, FakeProvider]:
    fake = FakeProvider(roles=roles)
    cfg = ForgeConfig(roles={role: [f"fake/{role}"] for role in roles})
    register_provider(cfg, fake)
    return make_ctx(root, cfg=cfg, headless=True), fake


async def test_a_second_task_sees_the_first_one(tmp_project: Path) -> None:
    ctx, fake = conversation_ctx(
        tmp_project,
        {
            "refiner": [trivial("a login page"), trivial("make it blue")],
            "coder": [FakeTurn(text="Built the login page in web/login.tsx."), FakeTurn(text="ok")],
        },
    )
    await run_task("Build a login page", ctx)
    assert conversation_text(ctx).count("<turn>") == 1
    await run_task("Now make it blue", ctx)
    refiner, coder = fake.requests[2], fake.requests[3]
    for request in (refiner, coder):
        assert "Build a login page" in request.system
        assert "Built the login page in web/login.tsx." in request.system
        assert "Now make it blue" not in request.system  # the new request is the task itself
    assert [t.prompt for t in ctx.session.conversation.turns] == [
        "Build a login page",
        "Now make it blue",
    ]
    sub = make_ctx(tmp_project, agent_id="explore-1")
    sub.session = ctx.session
    assert conversation_text(sub) == ""  # sub-agents get their task, not the conversation


async def test_old_turns_fold_into_the_notes(tmp_project: Path) -> None:
    ctx, fake = conversation_ctx(
        tmp_project, {"compressor": [FakeTurn(text="- the shop sells shoes")]}
    )
    long = "x" * (RESULT_CHARS + 500)  # cut to RESULT_CHARS
    for number in range(6):
        await record_turn(ctx, f"task {number}", True, long, ["shop.py"])
    talk = ctx.session.conversation
    assert talk.notes == "- the shop sells shoes"
    assert len(talk.turns) >= KEEP_TURNS and talk.turns[-1].prompt == "task 5"
    assert sum(len(t.result) for t in talk.turns) <= MAX_TURN_CHARS // 2
    [request] = fake.requests  # folded once, with room to spare for the next turns
    assert "task 0" in request.system and "task 5" not in request.system
    assert "- the shop sells shoes" in conversation_text(ctx)


async def test_compact_context_keeps_only_what_the_agent_says(tmp_project: Path) -> None:
    keep = "The user now works on the API only; tests run with `uv run pytest`."
    ctx, fake = conversation_ctx(
        tmp_project,
        {
            "coder": [
                FakeTurn(
                    tool_calls=[FakeToolCall(name="compact_context", arguments={"keep": keep})]
                ),
                FakeTurn(text="done"),
            ]
        },
    )
    ctx.session.conversation.turns.append(Turn(prompt="Style the shop's front page", result="Done"))
    events = ctx.bus.subscribe(ctx.session.id)
    result = await run_agent(ctx, "Add a /health endpoint")
    assert result.stopped == "done"
    talk = ctx.session.conversation
    assert (talk.notes, talk.turns) == (keep, [])
    first, second = fake.requests
    assert "Style the shop's front page" in first.system
    assert "Style the shop's front page" not in second.system and keep in second.system
    [stand_in] = second.messages  # the earlier messages are gone, the kept text stands in
    assert keep in stand_in.text() and "Add a /health endpoint" in stand_in.text()
    assert any(isinstance(e, Compacted) and e.level == 3 for e in await drain(events))


def test_the_prompts_tell_the_agent_to_compact_and_keep_markdown_notes() -> None:
    full = dict.fromkeys(prompts.KNOWN_SLOTS, "")
    for name in ("coder", "team_lead", "planner"):
        text = prompts.render(name, **full)
        assert "compact_context" in text
        for notes in ("FORGE.md", "docs/architecture.md", "docs/decisions.md", "docs/progress.md"):
            assert notes in text, (name, notes)


async def test_a_new_session_starts_empty_and_the_store_keeps_the_conversation(
    tmp_project: Path, tmp_path: Path
) -> None:
    store = SqliteStore(tmp_path / "forge.db")
    try:
        session = await store.create_session(str(tmp_project))
        assert session.conversation.turns == [] and session.conversation.notes == ""
        session.conversation.notes = "remember: dark mode"
        session.conversation.turns.append(Turn(prompt="p", result="r", files=["a.py"]))
        await store.save_session(session)
        loaded = await store.load_session(session.id)
        assert loaded.conversation == session.conversation
    finally:
        await store.close()
