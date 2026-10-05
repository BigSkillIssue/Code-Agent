"""Every Store (and BoardStore) implementation must behave the same."""

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from forge.local.json_store import JsonStore
from forge.local.memory_store import MemoryStore
from forge.local.sqlite_store import SqliteStore
from forge.plan import Plan, Step, TaskSpec
from forge.ports import BoardStore, SessionNotFoundError, Store
from forge.providers.base import text_message


@pytest.fixture(params=["memory", "sqlite", "json"])
async def store(request: pytest.FixtureRequest, tmp_path: Path) -> AsyncIterator[Store]:
    if request.param == "memory":
        yield MemoryStore()
    elif request.param == "sqlite":
        sqlite = SqliteStore(tmp_path / "forge.db")
        yield sqlite
        await sqlite.close()
    else:
        yield JsonStore(tmp_path / "json-store")


async def test_create_save_load_round_trip(store: Store) -> None:
    session = await store.create_session("/p")
    assert session.status == "active" and session.project_root == "/p" and len(session.id) == 32
    session.messages.append(text_message("user", "hello"))
    session.summary = "said hello"
    await store.save_session(session)
    loaded = await store.load_session(session.id)
    assert loaded == session
    loaded.summary = "changed"
    assert (await store.load_session(session.id)).summary == "said hello"  # loads are copies


async def test_unknown_session(store: Store) -> None:
    with pytest.raises(SessionNotFoundError):
        await store.load_session("0" * 32)


async def test_list_sessions_newest_first_per_project(store: Store) -> None:
    ids = []
    for n in range(3):
        session = await store.create_session("/p")
        session.created_at = 1000.0 + n
        await store.save_session(session)
        ids.append(session.id)
    await store.create_session("/other")
    listed = await store.list_sessions("/p")
    assert [s.id for s in listed] == list(reversed(ids))
    assert len(await store.list_sessions("/p", limit=2)) == 2


async def test_search_finds_messages_plans_and_summaries(store: Store) -> None:
    spec = TaskSpec(goal="g", context="", requirements=[], acceptance_criteria=["x"], size="small")
    first = await store.create_session("/p")
    first.messages = [text_message("user", "the refresh token lives in a cookie")]
    first.plan = Plan(
        spec=spec, steps=[Step(id="s1", title="Rotate token keys", detail="", check="x")]
    )
    await store.save_session(first)
    second = await store.create_session("/p")
    second.summary = "added a cookie banner"
    await store.save_session(second)
    elsewhere = await store.create_session("/other")
    elsewhere.summary = "token cookie"
    await store.save_session(elsewhere)
    hits = await store.search("/p", "token")
    assert [sid for sid, _ in hits] == [first.id]
    assert {sid for sid, _ in await store.search("/p", "cookie")} == {first.id, second.id}
    assert await store.search("/p", "   ") == []
    assert await store.search("/p", "nothing here") == []


async def test_board_claims_are_exclusive(store: Store) -> None:
    assert isinstance(store, BoardStore)
    assert await store.claim("s", "t1", "a1") == "a1"
    assert await store.claim("s", "t1", "a2") == "a1"
    assert await store.owners("s") == {"t1": "a1"}
    await store.release("s", "t1")
    assert await store.owners("s") == {}
    winners = await asyncio.gather(*(store.claim("s", "t2", f"a{n}") for n in range(4)))
    assert len(set(winners)) == 1
    assert await store.owners("other-session") == {}
