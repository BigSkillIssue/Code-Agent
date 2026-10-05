"""Read a Server-Sent Events stream (the wire format of streaming LLM APIs)."""

from collections.abc import AsyncIterator
from dataclasses import dataclass

import httpx


@dataclass
class ServerEvent:
    """One SSE event: its optional `event:` name and its joined `data:` lines."""

    event: str
    data: str


async def read_events(response: httpx.Response) -> AsyncIterator[ServerEvent]:
    """Yield events from a streaming response until it ends."""
    name, data = "", list[str]()
    async for line in response.aiter_lines():
        if not line:
            if data:
                yield ServerEvent(name, "\n".join(data))
            name, data = "", []
        elif line.startswith("data:"):
            data.append(line[5:].removeprefix(" "))
        elif line.startswith("event:"):
            name = line[6:].strip()
    if data:
        yield ServerEvent(name, "\n".join(data))
