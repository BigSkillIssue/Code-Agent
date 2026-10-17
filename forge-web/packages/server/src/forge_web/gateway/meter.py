"""What model calls cost: usage read from the upstream's own reply, prices from Forge's catalog,
monthly totals per user, and reservations for calls still running.

Usage is only ever taken from the upstream response, never from the sandbox. When a stream ends
without its final usage report (aborted), the call is charged an estimate: the larger of what the
upstream had reported so far and what the text that came through amounts to. Anthropic's prompt
cache is priced as Anthropic prices it (W42): reads at a tenth of the input price, writes at 1.25
times it (2 times for an hour's cache).
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
CACHE_READ, CACHE_WRITE, CACHE_WRITE_1H = 0.1, 1.25, 2.0  # times the input price (Anthropic)
CHARS_PER_TOKEN = 3
PRODUCT = "product:"  # the ledger's holders are users and, with this prefix, products


@dataclass
class Counted:
    """Tokens one call used."""

    input_tokens: int = 0  # all of them, from the prompt cache or not
    output_tokens: int = 0
    reported: bool = False
    cache_read: int = 0  # of the input: read from Anthropic's prompt cache
    cache_write: int = 0  # written to it for 5 minutes
    cache_write_1h: int = 0  # written to it for an hour


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
                self._json(line[5:].strip(), whole=False)

    def finish(self) -> Counted:
        """The usage found; for a non-streamed reply the whole body is read once more."""
        if self._pending.startswith(b"data:"):
            self._json(self._pending[5:].strip(), whole=False)
        if not self.counted.reported and self._plain.lstrip().startswith(b"{"):
            self._json(bytes(self._plain), whole=True)
        return self.counted

    def _json(self, raw: bytes, whole: bool) -> None:
        try:
            data = json.loads(raw)
        except (ValueError, UnicodeDecodeError):
            return
        if isinstance(data, dict):
            self._usage(data, whole)
            if not whole:
                self.output_chars += len(_text_of(data))

    def _usage(self, data: dict[str, Any], whole: bool) -> None:
        # Only a reply's final numbers count: early events (Anthropic's message_start says one
        # output token) would make a stream that is broken off on purpose almost free.
        c = self.counted
        if self.kind == "anthropic":
            usage = _dict(_dict(data.get("message")).get("usage") or data.get("usage"))
            if "input_tokens" in usage:
                read = int(usage.get("cache_read_input_tokens") or 0)
                written = int(usage.get("cache_creation_input_tokens") or 0)
                hour = _dict(usage.get("cache_creation")).get("ephemeral_1h_input_tokens")
                hour = min(int(hour or 0), written)
                c.input_tokens = int(usage.get("input_tokens") or 0) + read + written
                c.cache_read, c.cache_write, c.cache_write_1h = read, written - hour, hour
            if "output_tokens" in usage:
                c.output_tokens = int(usage["output_tokens"] or 0)
                c.reported = whole or data.get("type") == "message_delta"
        elif self.kind == "google":
            usage = _dict(data.get("usageMetadata"))
            if usage:
                c.input_tokens = int(usage.get("promptTokenCount") or 0)
                c.output_tokens = int(usage.get("candidatesTokenCount") or 0) + int(
                    usage.get("thoughtsTokenCount") or 0
                )
                finished = any(x.get("finishReason") for x in _dicts(data.get("candidates")))
                c.reported = whole or finished
        else:  # chat completions and responses report usage only at the end
            usage = _dict(data.get("usage") or _dict(data.get("response")).get("usage"))
            if usage:
                c.input_tokens = int(usage.get("prompt_tokens") or usage.get("input_tokens") or 0)
                c.output_tokens = int(
                    usage.get("completion_tokens") or usage.get("output_tokens") or 0
                )
                c.reported = True


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _dicts(value: Any) -> list[dict[str, Any]]:
    return [v for v in value if isinstance(v, dict)] if isinstance(value, list) else []


def _text_of(data: dict[str, Any]) -> str:
    """What one chunk generated, tool-call arguments and thinking included (for estimates)."""
    delta = data.get("delta")
    if isinstance(delta, str):  # responses API deltas
        return delta
    parts = [str(_dict(delta).get(k) or "") for k in ("text", "partial_json", "thinking")]
    for choice in _dicts(data.get("choices")):
        change = _dict(choice.get("delta"))
        parts += [str(change.get(k) or "") for k in ("content", "reasoning_content", "reasoning")]
        for call in _dicts(change.get("tool_calls")):
            parts.append(str(_dict(call.get("function")).get("arguments") or ""))
    for candidate in _dicts(data.get("candidates")):
        for part in _dicts(_dict(candidate.get("content")).get("parts")):
            parts.append(str(part.get("text") or ""))
            if "functionCall" in part:
                parts.append(json.dumps(part["functionCall"]))
    return "".join(parts)


def price(provider: str, model: str) -> tuple[float, float]:
    """USD per 1M input and output tokens."""
    caps = capabilities_for(provider, model)
    if caps.cost_in == 0 and caps.cost_out == 0:
        return FALLBACK_IN, FALLBACK_OUT
    return caps.cost_in, caps.cost_out


def priced(provider: str, model: str) -> bool:
    """Whether Forge knows what this model costs (else the server's keys are not used for it)."""
    caps = capabilities_for(provider, model)
    return not (caps.cost_in == 0 and caps.cost_out == 0)


def cost(provider: str, model: str, input_tokens: int, output_tokens: int,
         counted: Counted | None = None) -> float:  # fmt: skip
    """What a call costs (its cached input at the prompt cache's prices)."""
    cost_in, cost_out = price(provider, model)
    read, write, hour = (counted.cache_read, counted.cache_write,
                         counted.cache_write_1h) if counted else (0, 0, 0)  # fmt: skip
    plain = max(input_tokens - read - write - hour, 0)
    weighted = plain + read * CACHE_READ + write * CACHE_WRITE + hour * CACHE_WRITE_1H
    return (weighted * cost_in + output_tokens * cost_out) / 1_000_000


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

    async def spent_this_month(self, holder: str) -> float:
        """Server-key spending this month of a user, or of a product (`PRODUCT` + project id),
        from the database once, then kept."""
        start = month_start()
        if self.month.get(holder) != start:
            whose = (UsageRecord.project_id == holder.removeprefix(PRODUCT)
                     if holder.startswith(PRODUCT) else UsageRecord.user_id == holder)  # fmt: skip
            async with self.db.session() as session:
                total = await session.scalar(
                    select(func.coalesce(func.sum(UsageRecord.cost_usd), 0.0)).where(
                        whose, UsageRecord.key_kind == "server", UsageRecord.created_at >= start
                    )
                )
            self.spent[holder], self.month[holder] = float(total or 0.0), start
        return self.spent[holder]

    async def reserve(self, user_id: str, amount: float, limit: float | None,
                      project_id: str = "", product_limit: float | None = None) -> str:  # fmt: skip
        """Hold `amount` for a call if it fits under the user's monthly limit and the product's
        own; "" when it does, else which limit is used up (None: no limit)."""
        product = PRODUCT + project_id
        amount = max(amount, 0.0)  # a reservation never lowers what is held
        async with self.lock:
            spent = await self.spent_this_month(user_id)
            if limit is not None and spent + self.reserved.get(user_id, 0.0) + amount > limit:
                return "Your monthly limit for the server's API keys is used up."
            if project_id and product_limit is not None:
                used = await self.spent_this_month(product)
                if used + self.reserved.get(product, 0.0) + amount > product_limit:
                    return "This product's monthly AI budget is used up."
            for holder in (user_id, product) if project_id else (user_id,):
                self.reserved[holder] = self.reserved.get(holder, 0.0) + amount
            return ""

    async def settle(self, record: UsageRecord, reserved: float) -> None:
        """Replace a reservation by what the call cost, and store the record."""
        holders = [record.user_id] + ([PRODUCT + record.project_id] if record.project_id else [])
        async with self.lock:
            for holder in holders:
                if reserved:
                    self.reserved[holder] = max(0.0, self.reserved.get(holder, 0.0) - reserved)
                if record.key_kind == "server":
                    await self.spent_this_month(holder)
                    self.spent[holder] = self.spent.get(holder, 0.0) + record.cost_usd
        async with self.db.session() as session, session.begin():
            session.add(record)
