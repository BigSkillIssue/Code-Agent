"""Every EventBus implementation must deliver events the same way."""

import pytest

from forge.events import ErrorEvent, SessionDone
from forge.local.memory_bus import MemoryBus
from forge.ports import EventBus

BUSES = [MemoryBus]


@pytest.fixture(params=BUSES, ids=lambda cls: cls.__name__)
def bus(request: pytest.FixtureRequest) -> EventBus:
    return request.param()  # type: ignore[no-any-return]


def error(session: str, text: str) -> ErrorEvent:
    return ErrorEvent(session_id=session, ts=0.0, message=text)


async def test_session_subscribers_get_their_events_in_order(bus: EventBus) -> None:
    mine = bus.subscribe("s1")
    everything = bus.subscribe("*")
    for event in (error("s1", "one"), error("s2", "other"), error("s1", "two")):
        await bus.publish(event)
    await bus.publish(SessionDone(session_id="s1", ts=0.0, ok=True, report="r"))
    received = [e async for e in mine]  # ends after the session's SessionDone
    assert [getattr(e, "message", "done") for e in received] == ["one", "two", "done"]
    assert [getattr(await anext(everything), "message", "") for _ in range(3)] == [
        "one",
        "other",
        "two",
    ]


async def test_events_before_subscribing_are_not_delivered(bus: EventBus) -> None:
    await bus.publish(error("s1", "early"))
    late = bus.subscribe("s1")
    await bus.publish(error("s1", "late"))
    first = await anext(late)
    assert isinstance(first, ErrorEvent) and first.message == "late"
