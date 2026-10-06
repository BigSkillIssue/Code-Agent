"""FakeProvider: replays scripted model turns and records every request (offline tests)."""

import asyncio
import json
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from forge.providers.base import (
    Capabilities,
    ChatRequest,
    ErrorKind,
    Message,
    ProviderError,
    StreamItem,
    TextPart,
    ToolCall,
    Usage,
)
from forge.providers.tokens import estimate_tokens

EXHAUSTED = "(fake provider: script exhausted)"
CHUNK_CHARS = 16


class FakeToolCall(BaseModel):
    """A tool call the fake model makes."""

    name: str
    arguments: dict[str, Any] = {}
    id: str | None = None


class FakeTurn(BaseModel):
    """One scripted model answer: text, tool calls, or an error to raise."""

    text: str = ""
    tool_calls: list[FakeToolCall] = []
    usage: Usage | None = None
    error: ErrorKind | None = None  # with tool_calls: raised after the calls were streamed
    delay_s: float = 0.0  # pause after the tool calls, as if the model kept writing


class FakeScript(BaseModel):
    """A script file: shared turns, plus optional queues per model name (= role under --fake)."""

    turns: list[FakeTurn] = []
    roles: dict[str, list[FakeTurn]] = {}


TurnSource = FakeTurn | Callable[[ChatRequest], FakeTurn]


class FakeProvider:
    """A Provider whose answers come from a script instead of a model."""

    def __init__(
        self,
        turns: Sequence[TurnSource] = (),
        roles: Mapping[str, Sequence[TurnSource]] | None = None,
        *,
        caps: Capabilities | None = None,
        name: str = "fake",
    ) -> None:
        self.name = name
        self.requests: list[ChatRequest] = []
        self.caps = caps or Capabilities(context_window=128_000, max_output=8_192, vision=True)
        self._default: list[TurnSource] = list(turns)
        self._roles: dict[str, list[TurnSource]] = {k: list(v) for k, v in (roles or {}).items()}

    @classmethod
    def from_data(cls, data: Any) -> "FakeProvider":
        """Build from parsed JSON: a list of turns, or {"turns": [...], "roles": {...}}."""
        script = FakeScript.model_validate({"turns": data} if isinstance(data, list) else data)
        return cls(script.turns, script.roles)

    @classmethod
    def from_file(cls, path: Path) -> "FakeProvider":
        """Build from a JSON script file."""
        return cls.from_data(json.loads(path.read_text(encoding="utf-8")))

    def capabilities(self, model: str) -> Capabilities:
        """The same capabilities for every model name."""
        return self.caps

    async def count_tokens(self, req: ChatRequest) -> int:
        """Estimate, like providers without a counting endpoint."""
        return estimate_tokens(req)

    async def stream(self, req: ChatRequest) -> AsyncIterator[StreamItem]:
        """Answer with the next scripted turn for this model name."""
        self.requests.append(req)
        turn = self._next_turn(req)
        if turn.error and not turn.tool_calls:
            raise ProviderError(turn.error, "scripted failure")
        for start in range(0, len(turn.text), CHUNK_CHARS):
            yield StreamItem(delta=turn.text[start : start + CHUNK_CHARS])
        calls = [
            ToolCall(id=c.id or f"call_{i}", name=c.name, arguments=c.arguments)
            for i, c in enumerate(turn.tool_calls)
        ]
        for call in calls:
            yield StreamItem(tool_call=call)
        if turn.delay_s:
            await asyncio.sleep(turn.delay_s)
        if turn.error:
            raise ProviderError(turn.error, "scripted failure after the tool calls")
        parts = [TextPart(text=turn.text)] if turn.text else []
        usage = turn.usage or Usage(
            input_tokens=estimate_tokens(req), output_tokens=max(1, len(turn.text) // 4)
        )
        yield StreamItem(
            done=Message(role="assistant", parts=list(parts), tool_calls=calls), usage=usage
        )

    def remaining(self) -> int:
        """How many scripted turns have not been used yet."""
        return len(self._default) + sum(len(q) for q in self._roles.values())

    def _next_turn(self, req: ChatRequest) -> FakeTurn:
        queue = self._roles.get(req.model) or self._default
        if not queue:
            return FakeTurn(text=EXHAUSTED)
        source = queue.pop(0)
        return source if isinstance(source, FakeTurn) else source(req)
