"""What model calls cost: usage read from the upstream's own reply, prices from Forge's catalog,
monthly totals per user, and reservations for calls still running.

Usage is only ever taken from the upstream response, never from the sandbox. When a stream ends
without a usage report (aborted), the call is charged an estimate.
"""

import asyncio
import json
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from forge.providers.catalog import capabilities_for
from sqlalchemy import func, select

from forge_web.db.engine import Database
from forge_web.db.models import UsageRecord

# Price for models the catalog does not know, so limits still bite (USD per 1M tokens).
FALLBACK_IN, FALLBACK_OUT = 5.0, 25.0
CHARS_PER_TOKEN = 3


@dataclass
class Counted:
    """Tokens one call used."""

    input_tokens: int = 0
    output_tokens: int = 0
    reported: bool = False


class UsageSniffer:
    """Finds the usage numbers in a streamed (SSE) or plain JSON reply."""

    def __init__(self, kind: str) -> None:
        self.kind = kind
        self.counted = Counted()
        self.output_chars = 0
        self._pending = b""
        self._plain = bytearray()

    def feed(self, chunk: bytes) -> None:
        """Look at the next piece of the reply."""
        if len(self._plain) < 4_000_000:
            self._plain += chunk
        lines = (self._pending + chunk).split(b"\n")
        self._pending = lines.pop()
        for line in lines:
            if line.startswith(b"data:"):
                self._json(line[5:].strip())

    def finish(self) -> Counted:
        """The usage found; for a non-streamed reply the whole body is read once more."""
        if self._pending.startswith(b"data:"):
            self._json(self._pending[5:].strip())
        if not self.counted.reported and self._plain.lstrip().startswith(b"{"):
            self._json(bytes(self._plain))
        return self.counted

    def _json(self, raw: bytes) -> None:
        try:
            data = json.loads(raw)
        except (ValueError, UnicodeDecodeError):
            return
        if isinstance(data, dict):
            self._usage(data)
            self.output_chars += len(_text_of(data))

    def _usage(self, data: dict[str, Any]) -> None:
        c = self.counted
        if self.kind == "anthropic":
            usage = (data.get("message") or {}).get("usage") or data.get("usage") or {}
            if "input_tokens" in usage:
                c.input_tokens = (
                    int(usage.get("input_tokens") or 0)
                    + int(usage.get("cache_read_input_tokens") or 0)
                    + int(usage.get("cache_creation_input_tokens") or 0)
                )
            if "output_tokens" in usage:
                c.output_tokens = int(usage["output_tokens"] or 0)
                c.reported = True
        elif self.kind == "google":
            usage = data.get("usageMetadata") or {}
            if usage:
                c.input_tokens = int(usage.get("promptTokenCount") or 0)
                c.output_tokens = int(usage.get("candidatesTokenCount") or 0) + int(
                    usage.get("thoughtsTokenCount") or 0
                )
                c.reported = True
        else:
            usage = data.get("usage") or (data.get("response") or {}).get("usage") or {}
            if usage:
                c.input_tokens = int(usage.get("prompt_tokens") or usage.get("input_tokens") or 0)
                c.output_tokens = int(
                    usage.get("completion_tokens") or usage.get("output_tokens") or 0
                )
                c.reported = True


def _text_of(data: dict[str, Any]) -> str:
    """Generated text in one chunk (for estimating aborted streams)."""
    delta = data.get("delta")
    if isinstance(delta, dict) and isinstance(delta.get("text"), str):
        return str(delta["text"])
    if isinstance(delta, str):
        return delta
    choices = data.get("choices")
    if isinstance(choices, list) and choices and isinstance(choices[0], dict):
        content = (choices[0].get("delta") or {}).get("content")
        return content if isinstance(content, str) else ""
    return ""


def price(provider: str, model: str) -> tuple[float, float]:
    """USD per 1M input and output tokens."""
    caps = capabilities_for(provider, model)
    if caps.cost_in == 0 and caps.cost_out == 0:
        return FALLBACK_IN, FALLBACK_OUT
    return caps.cost_in, caps.cost_out


def cost(provider: str, model: str, input_tokens: int, output_tokens: int) -> float:
    """What a call costs."""
    cost_in, cost_out = price(provider, model)
    return (input_tokens * cost_in + output_tokens * cost_out) / 1_000_000


def month_start(now: float | None = None) -> float:
    """The first second of the current month (UTC)."""
    moment = datetime.fromtimestamp(now or time.time(), UTC)
    return moment.replace(day=1, hour=0, minute=0, second=0, microsecond=0).timestamp()


@dataclass
class Ledger:
    """Monthly spending on the server's keys per user, plus reservations in flight."""

    db: Database
    spent: dict[str, float] = field(default_factory=dict)
    month: dict[str, float] = field(default_factory=dict)
    reserved: dict[str, float] = field(default_factory=dict)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    async def spent_this_month(self, user_id: str) -> float:
        """Server-key spending of the user this month (from the database once, then kept)."""
        start = month_start()
        if self.month.get(user_id) != start:
            async with self.db.session() as session:
                total = await session.scalar(
                    select(func.coalesce(func.sum(UsageRecord.cost_usd), 0.0)).where(
                        UsageRecord.user_id == user_id,
                        UsageRecord.key_kind == "server",
                        UsageRecord.created_at >= start,
                    )
                )
            self.spent[user_id], self.month[user_id] = float(total or 0.0), start
        return self.spent[user_id]

    async def reserve(self, user_id: str, amount: float, limit: float) -> bool:
        """Hold `amount` for a call if it fits under the limit."""
        async with self.lock:
            spent = await self.spent_this_month(user_id)
            if spent + self.reserved.get(user_id, 0.0) + amount > limit:
                return False
            self.reserved[user_id] = self.reserved.get(user_id, 0.0) + amount
            return True

    async def settle(self, record: UsageRecord, reserved: float) -> None:
        """Replace a reservation by what the call cost, and store the record."""
        async with self.lock:
            if reserved:
                self.reserved[record.user_id] = max(
                    0.0, self.reserved.get(record.user_id, 0.0) - reserved
                )
            if record.key_kind == "server":
                await self.spent_this_month(record.user_id)
                self.spent[record.user_id] = self.spent.get(record.user_id, 0.0) + record.cost_usd
        async with self.db.session() as session, session.begin():
            session.add(record)
