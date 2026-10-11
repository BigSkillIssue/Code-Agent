"""Stripe's events (W31): each stored once (Stripe sends some again), then handled one at a time
in the order Stripe made them. An event older than one already applied to the same object is
recorded as superseded and changes nothing; an event without a handler is recorded as ignored.
A handler that fails is tried again later, a few times, while the events after it wait.
"""

import asyncio
import contextlib
import json
import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any

from sqlalchemy import func, select

from forge_web.db.billing_models import BillingEvent
from forge_web.db.engine import Database

log = logging.getLogger(__name__)
Handler = Callable[[dict[str, Any]], Awaitable[None]]
RETRY_S = 60.0  # how often waiting events are looked at again
MAX_ATTEMPTS = 5  # then the event counts as failed and the next ones go on


class Events:
    """Stores Stripe's events and hands them, in order, to the handlers of their types."""

    def __init__(self, db: Database) -> None:
        self.db = db
        self.handlers: dict[str, Handler] = {}
        self.busy = False
        self.again = False
        self.task: asyncio.Task[None] | None = None

    def on(self, kind: str, handler: Handler) -> None:
        """Handle the events of this type with `handler` (its argument: the whole event)."""
        self.handlers[kind] = handler

    def start(self) -> None:
        """Look at waiting events now and then (after a failed handler, say)."""
        self.task = asyncio.create_task(self.keep_going())

    async def close(self) -> None:
        if self.task is not None:
            self.task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.task

    async def keep_going(self) -> None:
        while True:
            try:
                await self.handle_pending()
            except Exception:  # a bad round must not end the handling
                log.exception("handling Stripe's events failed")
            await asyncio.sleep(RETRY_S)

    async def store(self, event: dict[str, Any]) -> bool:
        """Keep a new event; False when it was stored before."""
        obj = event.get("data", {}).get("object", {})
        row = BillingEvent(id=str(event["id"]), type=str(event["type"]),
                           object_id=str(obj.get("id", "")) if isinstance(obj, dict) else "",
                           created=float(event["created"]), livemode=bool(event.get("livemode")),
                           data=json.dumps(event), received_at=time.time())  # fmt: skip
        async with self.db.session() as session, session.begin():
            if await session.get(BillingEvent, row.id) is not None:
                return False
            session.add(row)
        return True

    async def handle_pending(self) -> list[str]:
        """Handle every waiting event, oldest first; the ids handled now. One round at a time:
        a call during a round makes that round look again before it ends."""
        if self.busy:
            self.again = True
            return []
        self.busy, done = True, []
        try:
            while True:
                self.again = False
                done += await self.one_round()
                if not self.again:
                    return done
        finally:
            self.busy = False

    async def one_round(self) -> list[str]:
        async with self.db.session() as session:
            waiting = list(await session.scalars(
                select(BillingEvent).where(BillingEvent.handled_at == 0)
                .order_by(BillingEvent.created, BillingEvent.received_at)))  # fmt: skip
        done = []
        for row in waiting:
            if not await self.handle(row):
                break  # it will be tried again; the events after it wait for it
            done.append(row.id)
        return done

    async def handle(self, row: BillingEvent) -> bool:
        """Handle one event; False when its handler failed and it is to be tried again."""
        outcome = await self.superseded(row) or ("ignored" if row.type not in self.handlers else "")
        if not outcome:
            try:
                await self.handlers[row.type](json.loads(row.data))
                outcome = "handled"
            except Exception as error:  # a handler's bug must not stop the others for good
                log.exception("handling Stripe event %s (%s) failed", row.id, row.type)
                outcome = f"failed: {error}"[:2000]
        failed, retry = outcome.startswith("failed"), False
        async with self.db.session() as session, session.begin():
            stored = await session.get(BillingEvent, row.id)
            if stored is not None:
                stored.attempts += 1
                stored.outcome = outcome
                if not failed or stored.attempts >= MAX_ATTEMPTS:
                    stored.handled_at = time.time()
                retry = failed and stored.attempts < MAX_ATTEMPTS
        return not retry

    async def superseded(self, row: BillingEvent) -> str:
        """ "superseded" when a newer event of the same object was applied already."""
        if not row.object_id:
            return ""
        async with self.db.session() as session:
            newest = await session.scalar(
                select(func.max(BillingEvent.created)).where(
                    BillingEvent.object_id == row.object_id, BillingEvent.handled_at > 0,
                    BillingEvent.outcome == "handled"))  # fmt: skip
        return "superseded" if newest is not None and newest > row.created else ""
