"""Helpers shared by tests: in-memory byte streams and connected multiplexers."""

import asyncio
import json
import os
import socket
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from forge_sandbox.daemon import Daemon
from forge_sandbox.mux import Mux, OpenHandler
from forge_sandbox.protocol import Notify
from forge_web.containers.driver import SandboxLink
from forge_web.sandbox_client import ForwardTarget, SandboxClient

Stream = tuple[asyncio.StreamReader, asyncio.StreamWriter]


async def stream_pair() -> tuple[Stream, Stream]:
    """Two connected byte streams (a socket pair; works on every OS)."""
    left, right = socket.socketpair()
    return await asyncio.open_connection(sock=left), await asyncio.open_connection(sock=right)


@asynccontextmanager
async def mux_pair(
    on_open: OpenHandler | None = None, *, window: int = 1024 * 1024
) -> AsyncIterator[tuple[Mux, Mux]]:
    """A started server-side and daemon-side Mux talking to each other."""
    (sr, sw), (dr, dw) = await stream_pair()
    server = Mux(sr, sw, role="server", window=window)
    daemon = Mux(dr, dw, role="daemon", on_open=on_open, window=window, info={"name": "test"})
    await asyncio.gather(server.start(), daemon.start())
    try:
        yield server, daemon
    finally:
        await server.close()
        await daemon.close()


async def connect(
    daemon: Daemon,
    targets: Mapping[str, ForwardTarget] | None = None,
    notes: asyncio.Queue[Notify] | None = None,
) -> tuple[SandboxClient, asyncio.Task[None]]:
    """A server-side client connected to `daemon` over an in-memory stream."""
    (sr, sw), (dr, dw) = await stream_pair()
    serving = asyncio.create_task(daemon.serve_connection(dr, dw))

    async def close_link() -> None:
        sw.close()

    async def on_notify(message: Notify) -> None:
        if notes is not None:
            notes.put_nowait(message)

    client = SandboxClient(SandboxLink(sr, sw, close_link), targets=targets, on_notify=on_notify)
    await client.start()
    return client, serving


@asynccontextmanager
async def sandbox(
    root: Path,
    targets: Mapping[str, ForwardTarget] | None = None,
    notes: asyncio.Queue[Notify] | None = None,
    env: Mapping[str, str] | None = None,
) -> AsyncIterator[tuple[Daemon, SandboxClient]]:
    daemon = Daemon(root, env={**os.environ, **(env or {})})
    client, serving = await connect(daemon, targets, notes)
    try:
        yield daemon, client
    finally:
        await client.close()
        serving.cancel()
        await daemon.close()


TRIVIAL_SPEC = json.dumps(
    {
        "goal": "answer the user",
        "context": "",
        "requirements": [],
        "constraints": [],
        "acceptance_criteria": ["the user has an answer"],
        "assumptions": [],
        "open_questions": [],
        "size": "trivial",
    }
)


def fake_script(*turns: dict[str, Any], prompts: int = 1) -> dict[str, Any]:
    """A FakeProvider script: `prompts` trivial refinements, then the coder's turns."""
    return {"roles": {"refiner": [{"text": TRIVIAL_SPEC}] * prompts}, "turns": list(turns)}


def call(name: str, **arguments: Any) -> dict[str, Any]:
    """A scripted model turn that calls one tool."""
    return {"text": "", "tool_calls": [{"name": name, "arguments": arguments}]}
