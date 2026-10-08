"""The server's side of a sandbox connection: calls, channels and the targets it serves.

Everything that comes back from a sandbox is untrusted: results are validated by the caller, and
the only channels a sandbox may open are `forward` channels to targets the server listed.
"""

import asyncio
import contextlib
from collections.abc import Awaitable, Callable, Mapping
from typing import Any

from forge_sandbox.methods import ForwardArgs, parse_params
from forge_sandbox.mux import Channel, Mux, OpenRefused
from forge_sandbox.protocol import Hello, Notify
from forge_sandbox.rpc import Rpc, RpcError
from forge_sandbox.streams import pump
from forge_web.containers.driver import SandboxLink

ForwardTarget = Callable[[], Awaitable[tuple[asyncio.StreamReader, asyncio.StreamWriter]]]
NotifyHandler = Callable[[Notify], Awaitable[None]]


class SandboxClient:
    """One connection to one project's sandbox daemon."""

    def __init__(
        self,
        link: SandboxLink,
        *,
        targets: Mapping[str, ForwardTarget] | None = None,
        on_notify: NotifyHandler | None = None,
    ) -> None:
        self.link = link
        self.targets = dict(targets or {})
        self.mux = Mux(link.reader, link.writer, role="server", on_open=self._on_open)
        self.rpc = Rpc(self.mux.control, {}, on_notify)
        self.hello: Hello | None = None
        self._loop: asyncio.Task[None] | None = None

    async def start(self, timeout: float = 30) -> Hello:
        """Handshake with the daemon."""
        self.hello = await self.mux.start(timeout)
        self._loop = asyncio.create_task(self.rpc.run())
        return self.hello

    @property
    def closed(self) -> bool:
        """The connection is gone."""
        return self.mux.closed

    async def call(
        self, method: str, params: dict[str, Any] | None = None, timeout: float = 60
    ) -> Any:
        """Call a daemon method; RpcError if it failed."""
        return await self.rpc.call(method, params, timeout)

    async def open(self, kind: str, args: dict[str, Any] | None = None) -> Channel:
        """Open a channel on the daemon (a terminal, a connection into the sandbox, ...)."""
        return await self.mux.open(kind, args)

    async def _on_open(self, channel: Channel) -> None:
        if channel.kind != "forward":
            raise OpenRefused("forbidden", f"a sandbox may not open {channel.kind!r} channels")
        try:
            target_name = parse_params(ForwardArgs, channel.args).target
        except RpcError as err:
            raise OpenRefused(err.code, err.message) from None
        target = self.targets.get(target_name)
        if target is None:
            raise OpenRefused("unknown_target", f"no forward target {target_name!r}")
        try:
            reader, writer = await target()
        except OSError:
            raise OpenRefused("connect_failed", f"{target_name} is not reachable") from None
        await channel.accept()
        await pump(channel, reader, writer)

    async def wait_closed(self) -> None:
        """Wait until the connection ends."""
        await self.mux.wait_closed()

    async def close(self) -> None:
        """Close the connection and release the stream."""
        await self.mux.close()
        if self._loop is not None:
            self._loop.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._loop
        await self.link.close()
