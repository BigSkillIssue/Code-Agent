"""One task writes every chat event, in batches, so many busy chats do not fight over SQLite."""

import asyncio
import contextlib
import logging
from dataclasses import dataclass

from sqlalchemy import insert

from forge_web.db.engine import Database
from forge_web.db.models import ChatEvent

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class EventRow:
    """A chat event waiting to be written."""

    chat_id: str
    seq: int
    dseq: int
    ts: float
    type: str
    kind: str
    data: str


# Rows waiting to be written; with more, chat relays wait (and their sandboxes with them).
MAX_WAITING = 10_000


class EventWriter:
    """Collects rows and commits them together, a batch at a time."""

    def __init__(self, db: Database, *, batch: int = 500, linger: float = 0.05) -> None:
        self.db = db
        self.batch = batch
        self.linger = linger
        self._queue: asyncio.Queue[EventRow] = asyncio.Queue()
        self._idle = asyncio.Event()
        self._idle.set()
        self._task: asyncio.Task[None] | None = None

    def start(self) -> None:
        """Start the writing task."""
        self._task = asyncio.create_task(self._run())

    def add(self, row: EventRow) -> None:
        """Queue a row; it is committed within `linger` seconds."""
        self._idle.clear()
        self._queue.put_nowait(row)

    async def room(self) -> None:
        """Wait while too many rows wait to be written."""
        while self._queue.qsize() >= MAX_WAITING:
            await asyncio.sleep(self.linger)

    async def flush(self) -> None:
        """Wait until every queued row is committed."""
        await self._idle.wait()

    async def _run(self) -> None:
        while True:
            rows = [await self._queue.get()]
            await asyncio.sleep(self.linger)
            while len(rows) < self.batch and not self._queue.empty():
                rows.append(self._queue.get_nowait())
            try:
                await self._write(rows)
            except Exception:
                log.exception("could not store %d chat events", len(rows))
            if self._queue.empty():
                self._idle.set()

    async def _write(self, rows: list[EventRow]) -> None:
        values = [row.__dict__ for row in rows]
        async with self.db.session() as session, session.begin():
            await session.execute(insert(ChatEvent), values)

    async def close(self) -> None:
        """Write what is queued, then stop."""
        if self._task is None:
            return
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self.flush(), 10)
        self._task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self._task
