"""TCP forwarding through the server connection, in both directions.

- Out of the sandbox: the daemon listens on 127.0.0.1:<port> for a named server target (the LLM
  gateway, the egress proxy) and turns every connection into a `forward` channel to the server.
  This works even when the sandbox has no network at all.
- Into the sandbox: the server opens a `connect` channel to a port on the sandbox's loopback
  interface (the live preview of a dev server).
"""

import asyncio
import contextlib
from collections.abc import Awaitable, Callable
from typing import Any

from forge_sandbox.methods import ConnectArgs, ListenParams, method, parse_params
from forge_sandbox.mux import Channel, ChannelClosed, OpenFailed, OpenRefused
from forge_sandbox.rpc import Handler, RpcError
from forge_sandbox.streams import pump

Opener = Callable[[str, dict[str, Any]], Awaitable[Channel]]
LOOPBACK = "127.0.0.1"
LOCAL_ADDRESSES = (LOOPBACK, "::1")


class Forwards:
    """Listeners that lead out to server targets, and connections the server makes in."""

    def __init__(self, opener: Opener) -> None:
        self.opener = opener
        self.listeners: dict[str, tuple[asyncio.Server, int]] = {}
        self._tasks: set[asyncio.Task[None]] = set()

    def handlers(self) -> dict[str, Handler]:
        """forward.* methods."""
        return {"forward.listen": method(ListenParams, self.listen)}

    def ports(self) -> set[int]:
        """The ports the daemon itself listens on."""
        return {port for _, port in self.listeners.values()}

    async def listen(self, params: ListenParams) -> dict[str, Any]:
        """Listen for `target` on 127.0.0.1:`port` (0 = any free port); idempotent."""
        current = self.listeners.get(params.target)
        if current is not None and params.port in (0, current[1]):
            return {"target": params.target, "port": current[1]}
        if current is not None:
            current[0].close()

        async def accepted(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            await self._out(params.target, reader, writer)

        try:
            server = await asyncio.start_server(accepted, LOOPBACK, params.port)
        except OSError as err:
            raise RpcError("listen_failed", f"port {params.port}: {err.strerror}") from None
        port = int(server.sockets[0].getsockname()[1])
        self.listeners[params.target] = (server, port)
        return {"target": params.target, "port": port}

    async def _out(
        self, target: str, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        try:
            channel = await self.opener("forward", {"target": target})
        except (OpenFailed, ChannelClosed, ConnectionError):
            writer.close()  # no server connected, or the target refused
            return
        await pump(channel, reader, writer)

    async def connect(self, channel: Channel) -> None:
        """Serve a `connect` channel: bytes to and from 127.0.0.1:`port` in the sandbox."""
        port = parse_params(ConnectArgs, channel.args).port
        if port in self.ports():
            raise OpenRefused("forbidden", "that port belongs to the daemon")
        for address in LOCAL_ADDRESSES:  # dev servers such as Vite may listen on ::1 only
            try:
                reader, writer = await asyncio.open_connection(address, port)
                break
            except OSError:
                continue
        else:
            raise OpenRefused("connect_failed", f"nothing listens on port {port}")
        await channel.accept()
        await pump(channel, reader, writer)

    async def close(self) -> None:
        """Stop listening."""
        for server, _ in self.listeners.values():
            server.close()
            with contextlib.suppress(Exception):
                await asyncio.wait_for(server.wait_closed(), 1)
        self.listeners.clear()
