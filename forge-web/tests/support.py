"""Helpers shared by tests: in-memory byte streams and connected multiplexers."""

import asyncio
import socket
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from forge_sandbox.mux import Mux, OpenHandler

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
