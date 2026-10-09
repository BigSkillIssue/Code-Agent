"""Following chats: when run tokens work, and how much a hostile sandbox or a slow browser may
make the server keep."""

import asyncio
import time
from pathlib import Path
from typing import Any

import pytest

from forge_web.chats import runs as runs_module
from forge_web.chats.runs import LiveChat, RunManager
from forge_web.db.engine import Database
from forge_web.db.models import Chat, ChatEvent, Project, User
from forge_web.hub import MAX_QUEUED_BYTES, Hub, Subscriber


async def test_the_sandbox_alone_cannot_keep_a_run_token_alive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = [1000.0]
    monkeypatch.setattr(runs_module.time, "monotonic", lambda: now[0])
    runs = RunManager(None, None, None, Hub(), lambda chat, info: {}, run_seconds=60)  # type: ignore[arg-type]

    async def save(chat_id: str, **values: Any) -> None:
        pass

    monkeypatch.setattr(runs, "_save", save)
    live = LiveChat("c1", "p1", "u1", next_seq=1, dseq=0, boot="", state="idle", title="T")
    runs.lives["c1"] = live
    await runs._track(live, {"type": "status", "state": "running"})  # the sandbox says so
    assert live.state == "running" and not runs.working("c1")
    runs.allow_run(live)  # a person sent a message
    assert runs.working("c1")
    await runs._track(live, {"type": "turn"})
    await runs._track(live, {"type": "status", "state": "running"})
    assert not runs.working("c1")  # the turn ended: a later "running" opens nothing
    runs.allow_run(live)
    now[0] += 61
    assert live.state == "running" and not runs.working("c1")  # at most run_seconds
    assert not runs.working("nobody")


async def test_live_state_from_a_hostile_sandbox_stays_small(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runs = RunManager(None, None, None, Hub(), lambda chat, info: {})  # type: ignore[arg-type]

    async def save(chat_id: str, **values: Any) -> None:
        pass

    monkeypatch.setattr(runs, "_save", save)
    live = LiveChat("c1", "p1", "u1", next_seq=1, dseq=0, boot="", state="running", title="T")

    def delta(agent: str, text: str) -> dict[str, Any]:
        return {"event": {"kind": "model_delta", "agent_id": agent, "text": text}}

    def output(call: str, text: str) -> dict[str, Any]:
        return {"event": {"kind": "tool_output", "call_id": call, "text": text}}

    kept = [runs._stream(live, delta(f"agent-{n}", "x" * 1000)) for n in range(100)]
    assert kept.count(True) == runs_module.MAX_LIVE_ENTRIES == len(live.streaming)
    assert not runs._stream(live, delta("a" * 65, "x"))  # made-up ids of any length
    assert runs._stream(live, delta("agent-0", "y" * 300_000))  # known ids go on
    assert len(live.streaming["agent-0"]) == runs_module.MAX_STREAMING
    for _ in range(600):
        runs._stream(live, output("call", "z" * 1000))
    assert sum(map(len, live.outputs["call"])) <= runs_module.MAX_STREAMING
    for n in range(200):
        await runs._track(live, {"type": "request", "id": f"r{n}"})
    assert len(live.pending) == runs_module.MAX_PENDING and "r199" in live.pending


async def test_stored_history_is_limited_per_project(tmp_path: Path) -> None:
    db = Database(f"sqlite+aiosqlite:///{tmp_path / 'runs.db'}")
    await db.migrate()
    now = time.time()
    async with db.session() as session, session.begin():
        session.add(User(id="u1", email="a@x", role="member", status="active", created_at=now))
        await session.flush()
        session.add(Project(id="p1", name="P", owner_id="u1", created_at=now, updated_at=now))
        await session.flush()
        session.add(Chat(id="c1", project_id="p1", user_id="u1", created_at=now, updated_at=now))
        await session.flush()
        session.add(ChatEvent(chat_id="c1", seq=1, dseq=1, ts=now, type="user", data="x" * 600))
    runs = RunManager(db, None, None, Hub(), lambda c, i: {}, max_log_bytes=1000)  # type: ignore[arg-type]
    live = LiveChat("c1", "p1", "u1", next_seq=2, dseq=1, boot="", state="running", title="T")
    assert await runs._may_store(live, 300)  # 600 stored before + 300
    assert not await runs._may_store(live, 200) and live.log_full  # over 1000
    unlimited = RunManager(db, None, None, Hub(), lambda c, i: {})  # type: ignore[arg-type]
    assert await unlimited._may_store(live, 100_000)
    assert not await unlimited._may_store(live, runs_module.MAX_STORED_ITEM + 1)
    await db.close()


async def test_a_browser_that_does_not_read_is_dropped_by_size() -> None:
    subscriber = Subscriber("u1")
    big = {"type": "live", "chat_id": "c1", "text": "x" * (1024 * 1024)}
    for _ in range(MAX_QUEUED_BYTES // (1024 * 1024) // 2 + 1):
        subscriber.put(big)
    room = asyncio.create_task(subscriber.room())
    await asyncio.sleep(0)
    assert not room.done()  # a replay would wait here
    while subscriber.queued_bytes > MAX_QUEUED_BYTES // 2:
        assert await subscriber.next() is not None
    await asyncio.wait_for(room, 1)
    for _ in range(MAX_QUEUED_BYTES // (1024 * 1024) + 1):
        subscriber.put(big)
    assert subscriber.overflowed  # long before 20 000 messages
    subscriber.hold("c2")
    subscriber.put({"type": "item", "chat_id": "c2", "seq": 1})
    assert subscriber.held_bytes == 0  # nothing more is kept once it overflowed


def test_a_sandbox_with_another_forge_is_reported_once(caplog: pytest.LogCaptureFixture) -> None:
    runs = RunManager(None, None, None, Hub(), lambda c, i: {})  # type: ignore[arg-type]
    runs.note_forge("p1", runs.forge)
    assert not caplog.records  # the same Forge: nothing to say
    for _ in range(3):
        runs.note_forge("p2", "0123456789ab")
    warned = [r for r in caplog.records if "runs Forge 0123456789ab" in r.getMessage()]
    assert len(warned) == 1 and runs.other_forge == {"p2"}
