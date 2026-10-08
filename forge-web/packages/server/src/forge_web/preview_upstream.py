"""Connections from the preview proxy to an app in a project's sandbox.

Each connection is a `connect` channel through the sandbox connection (the sandbox itself has
no network), spoken to with httpcore. No connection is kept alive: nothing outlives a request.
"""

import asyncio
import contextlib
from collections.abc import Awaitable, Callable, Iterable
from typing import Any

import httpcore

from forge_sandbox.mux import Channel, ChannelClosed, OpenFailed
from forge_web.containers.driver import SandboxError
from forge_web.preview_auth import Target
from forge_web.services import Services

OpenPort = Callable[[int], Awaitable[Channel]]
TIMEOUTS = {"connect": 15.0, "read": None, "write": 60.0, "pool": 15.0}


class NothingListening(httpcore.ConnectError):
    """No program listens on that port in the sandbox."""


class SandboxDown(httpcore.ConnectError):
    """The project's sandbox cannot be reached."""


class ChannelStream(httpcore.AsyncNetworkStream):
    """A `connect` channel as a byte stream for httpcore."""

    def __init__(self, channel: Channel) -> None:
        self.channel = channel
        self.pending = b""

    async def read(self, max_bytes: int, timeout: float | None = None) -> bytes:
        """Up to `max_bytes`; b"" once the app closed the connection."""
        if not self.pending:
            try:
                self.pending = await asyncio.wait_for(self.channel.read(), timeout)
            except TimeoutError:
                raise httpcore.ReadTimeout("the app did not answer in time") from None
        data, self.pending = self.pending[:max_bytes], self.pending[max_bytes:]
        return data

    async def write(self, buffer: bytes, timeout: float | None = None) -> None:
        """Send bytes to the app."""
        try:
            await asyncio.wait_for(self.channel.send(buffer), timeout)
        except ChannelClosed:
            raise httpcore.WriteError("the app closed the connection") from None
        except TimeoutError:
            raise httpcore.WriteTimeout("the app does not read") from None

    async def aclose(self) -> None:
        """Close the channel."""
        with contextlib.suppress(Exception):
            await self.channel.close()

    async def start_tls(self, *_args: Any, **_kwargs: Any) -> httpcore.AsyncNetworkStream:
        """Previews speak plain HTTP inside the sandbox."""
        raise httpcore.ConnectError("no TLS to apps in the sandbox")

    def get_extra_info(self, info: str) -> Any:
        """Nothing to tell (no socket)."""
        return None


class SandboxBackend(httpcore.AsyncNetworkBackend):
    """Connections to ports inside one project's sandbox."""

    def __init__(self, open_port: OpenPort) -> None:
        self.open_port = open_port

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: Iterable[Any] | None = None,
    ) -> ChannelStream:
        """A new channel to `port` on the sandbox's loopback interface (the host is ignored)."""
        try:
            channel = await asyncio.wait_for(self.open_port(port), timeout)
        except OpenFailed as err:
            raise NothingListening(f"nothing listens on port {port}: {err.info.message}") from None
        except (ChannelClosed, SandboxError, OSError, TimeoutError):
            raise SandboxDown("the project's sandbox is not reachable") from None
        return ChannelStream(channel)

    async def connect_unix_socket(self, *_args: Any, **_kwargs: Any) -> ChannelStream:
        """Not used."""
        raise httpcore.ConnectError("no unix sockets in previews")

    async def sleep(self, seconds: float) -> None:
        """Wait."""
        await asyncio.sleep(seconds)


def project_ports(services: Services, project_id: str) -> OpenPort:
    """Opens `connect` channels into one project's sandbox."""

    async def open_port(port: int) -> Channel:
        client = await services.runs.link(project_id)
        return await client.open("connect", {"port": port})

    return open_port


def upstream(services: Services, target: Target) -> httpcore.AsyncHTTPConnection:
    """A one-request connection to the app."""
    backend = SandboxBackend(project_ports(services, target.project_id))
    origin = httpcore.Origin(b"http", b"localhost", target.port)
    return httpcore.AsyncHTTPConnection(origin, network_backend=backend)
