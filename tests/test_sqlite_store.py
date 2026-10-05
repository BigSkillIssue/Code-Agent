"""The same Store tests for MemoryStore and SqliteStore."""

from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from forge.local.memory_store import MemoryStore
from forge.local.sqlite_store import SqliteStore
from forge.plan import Plan, Step, TaskSpec
from forge.ports import SessionNotFoundError, Store
from forge.providers.base import text_message

SPEC = TaskSpec(
    goal="Add JWT auth", context="c", requirements=[], acceptance_criteria=["a"], size="small"
)


@pytest.fixture(params=["memory", "sqlite"])
async def store(request: pytest.FixtureRequest, tmp_path: Path) -> AsyncIterator[Store]:
    if request.param == "memory":
        yield MemoryStore()
        return
    sqlite = SqliteStore(tmp_path / "forge.db")
    yield sqlite
    await sqlite.close()


async def test_round_trip(store: Store) -> None:
    session = await store.create_session("/p")
    session.spec = SPEC
    session.plan = Plan(
        spec=SPEC,
        steps=[Step(id="s1", title="Add Token model", detail="d", check="pytest", status="done")],
    )
    session.messages.append(text_message("user", "add auth"))
    session.status = "waiting"
    await store.save_session(session)
    loaded = await store.load_session(session.id)
    assert loaded == session


async def test_missing_session(store: Store) -> None:
    with pytest.raises(SessionNotFoundError):
        await store.load_session("nope")


async def test_list_newest_first(store: Store) -> None:
    first = await store.create_session("/p")
    second = await store.create_session("/p")
    second.created_at = first.created_at + 5
    await store.save_session(second)
    await store.create_session("/other")
    assert [s.id for s in await store.list_sessions("/p")] == [second.id, first.id]
    assert len(await store.list_sessions("/p", limit=1)) == 1


async def test_search_messages_summaries_and_steps(store: Store) -> None:
    old = await store.create_session("/p")
    old.messages.append(
        text_message(
            "assistant", "the refresh token lives in an httpOnly cookie, decided because of XSS"
        )
    )
    old.summary = "decided on JWT"
    old.plan = Plan(spec=SPEC, steps=[Step(id="s1", title="Add Token model", detail="", check="x")])
    await store.save_session(old)
    other = await store.create_session("/elsewhere")
    other.summary = "cookie"
    await store.save_session(other)
    hits = await store.search("/p", "httpOnly cookie")
    assert hits and hits[0][0] == old.id and "httpOnly cookie" in hits[0][1]
    assert (await store.search("/p", "token model"))[0][0] == old.id
    assert await store.search("/p", "nonexistentword") == []


async def test_search_ignores_operators(store: Store) -> None:
    session = await store.create_session("/p")
    session.summary = "uses OR and NOT words"
    await store.save_session(session)
    assert (await store.search("/p", 'OR "NOT'))[0][0] == session.id


async def test_sqlite_survives_reopening(tmp_path: Path) -> None:
    first = SqliteStore(tmp_path / "forge.db")
    session = await first.create_session("/p")
    await first.close()
    again = SqliteStore(tmp_path / "forge.db")
    assert (await again.load_session(session.id)).id == session.id
    await again.close()
