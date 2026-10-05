"""An in-process EventBus: every subscriber gets its own asyncio queue."""

import asyncio

from forge.events import Event, SessionDone

ALL_SESSIONS = "*"


class Subscription:
    """Async iterator over one subscriber's events; ends after its session's SessionDone."""

    def __init__(self, bus: "MemoryBus", session_id: str, queue: asyncio.Queue[Event]) -> None:
        self._bus = bus
        self._session_id = session_id
        self._queue = queue
        self._finished = False

    def __aiter__(self) -> "Subscription":
        return self

    async def __anext__(self) -> Event:
        if self._finished:
            raise StopAsyncIteration
        event = await self._queue.get()
        if isinstance(event, SessionDone) and self._session_id != ALL_SESSIONS:
            await self.aclose()
        return event

    async def aclose(self) -> None:
        """Stop receiving events."""
        self._finished = True
        self._bus.unsubscribe(self._session_id, self._queue)


class MemoryBus:
    """Delivers each published event to every subscriber of its session (and of `*`)."""

    def __init__(self) -> None:
        self._queues: dict[str, list[asyncio.Queue[Event]]] = {}

    async def publish(self, event: Event) -> None:
        """Queue the event for all matching subscribers."""
        for key in (event.session_id, ALL_SESSIONS):
            for queue in self._queues.get(key, []):
                queue.put_nowait(event)

    def subscribe(self, session_id: str) -> Subscription:
        """Start receiving a session's events now (`*` = every session)."""
        queue: asyncio.Queue[Event] = asyncio.Queue()
        self._queues.setdefault(session_id, []).append(queue)
        return Subscription(self, session_id, queue)

    def unsubscribe(self, session_id: str, queue: asyncio.Queue[Event]) -> None:
        """Forget one subscriber's queue."""
        queues = self._queues.get(session_id, [])
        if queue in queues:
            queues.remove(queue)

    def subscriber_count(self, session_id: str) -> int:
        """How many subscribers a session has."""
        return len(self._queues.get(session_id, []))
