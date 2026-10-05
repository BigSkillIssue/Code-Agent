"""Tests for the port data models and the in-memory bus and store."""

import asyncio

import pytest

from forge.events import ErrorEvent, ModelDelta, SessionDone
from forge.local.memory_bus import MemoryBus
from forge.local.memory_store import MemoryStore
from forge.plan import Plan, Step, TaskSpec
from forge.ports import Command, CommandResult, SandboxPolicy, Session, SessionNotFoundError
from forge.providers.base import text_message


def delta(session: str, text: str) -> ModelDelta:
    return ModelDelta(session_id=session, ts=0.0, text=text)


async def test_publish_subscribe_keeps_order() -> None:
    bus = MemoryBus()
    sub = bus.subscribe("s1")
    for i in range(5):
        await bus.publish(delta("s1", str(i)))
    await bus.publish(SessionDone(session_id="s1", ts=0.0, ok=True, report=""))
    received = [event async for event in sub]
    assert [getattr(e, "text", "end") for e in received] == ["0", "1", "2", "3", "4", "end"]


async def test_subscriber_only_sees_its_session() -> None:
    bus = MemoryBus()
    sub = bus.subscribe("s1")
    await bus.publish(delta("other", "x"))
    await bus.publish(delta("s1", "mine"))
    event = await asyncio.wait_for(anext(sub), timeout=1)
    assert isinstance(event, ModelDelta)
    assert event.text == "mine"


async def test_every_subscriber_gets_every_event() -> None:
    bus = MemoryBus()
    first, second, everything = bus.subscribe("s"), bus.subscribe("s"), bus.subscribe("*")
    await bus.publish(ErrorEvent(session_id="s", ts=0.0, message="boom"))
    for sub in (first, second, everything):
        event = await asyncio.wait_for(anext(sub), timeout=1)
        assert isinstance(event, ErrorEvent)


async def test_closed_subscription_stops_receiving() -> None:
    bus = MemoryBus()
    sub = bus.subscribe("s")
    await sub.aclose()
    await bus.publish(delta("s", "late"))
    assert bus.subscriber_count("s") == 0


async def test_store_round_trip_is_a_copy() -> None:
    store = MemoryStore()
    session = await store.create_session("/project")
    assert len(session.id) == 32
    assert session.status == "active"
    spec = TaskSpec(goal="g", context="c", requirements=[], acceptance_criteria=["a"], size="small")
    session.plan = Plan(spec=spec, steps=[Step(id="s1", title="t", detail="d", check="true")])
    session.messages.append(text_message("user", "fix the login bug"))
    await store.save_session(session)
    session.messages.clear()  # later changes must not leak into the store
    loaded = await store.load_session(session.id)
    assert loaded.plan is not None and loaded.plan.steps[0].id == "s1"
    assert loaded.messages[0].text() == "fix the login bug"


async def test_store_missing_session_raises() -> None:
    with pytest.raises(SessionNotFoundError):
        await MemoryStore().load_session("nope")


async def test_list_sessions_newest_first_and_per_project() -> None:
    store = MemoryStore()
    first = await store.create_session("/a")
    second = await store.create_session("/a")
    await store.create_session("/b")
    second.created_at = first.created_at + 10
    await store.save_session(second)
    listed = await store.list_sessions("/a")
    assert [s.id for s in listed] == [second.id, first.id]
    assert len(await store.list_sessions("/a", limit=1)) == 1


async def test_search_finds_messages_and_summaries() -> None:
    store = MemoryStore()
    old = await store.create_session("/p")
    old.messages.append(text_message("user", "the refresh token lives in an httpOnly cookie"))
    old.summary = "decided on JWT"
    await store.save_session(old)
    hits = await store.search("/p", "httpOnly cookie")
    assert hits and hits[0][0] == old.id
    assert "httpOnly cookie" in hits[0][1]
    assert await store.search("/p", "jwt") == [(old.id, "decided on JWT")]
    assert await store.search("/other", "cookie") == []


def test_port_models_have_contract_defaults() -> None:
    cmd = Command(cwd="/tmp")
    assert (cmd.argv, cmd.script, cmd.shell, cmd.timeout_s, cmd.env) == (
        None,
        None,
        "none",
        120,
        {},
    )
    result = CommandResult(exit_code=0, stdout="", stderr="")
    assert (result.timed_out, result.sandbox_denied, result.job_id) == (False, False, None)
    policy = SandboxPolicy()
    assert (policy.mode, policy.writable_roots, policy.network) == ("workspace-write", [], False)
    session = Session(id="x", project_root="/p", created_at=0.0, status="waiting")
    assert (session.spec, session.plan, session.messages, session.summary) == (None, None, [], "")
