"""Which addresses count as public, and an httpx transport that only connects to those.

`web_fetch` checks a URL's addresses before it fetches, but a name can resolve to another
address a moment later (DNS rebinding). The transport resolves the name itself when it connects
and refuses local, private and other non-public addresses, so the address checked is the address
used. Requests through a proxy (HTTPS_PROXY) are resolved by the proxy and keep only the first
check.
"""

import asyncio
import ipaddress
import socket
from collections.abc import Awaitable, Callable, Iterable

import httpcore
import httpx

Resolver = Callable[[str], Awaitable[list[str]]]


async def resolve_host(host: str) -> list[str]:
    """IP addresses of `host` (DNS)."""
    infos = await asyncio.get_running_loop().getaddrinfo(host, None, type=socket.SOCK_STREAM)
    return [str(info[4][0]) for info in infos]


def is_ip(host: str) -> bool:
    """True for an IPv4 or IPv6 literal."""
    try:
        ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        return False
    return True


def is_local(address: str) -> bool:
    """True unless the address is globally reachable: loopback, private, link-local (cloud
    metadata), shared (CGNAT, 100.64/10), multicast, reserved, unspecified, and IPv6 forms
    that carry one of those (IPv4-mapped, 6to4, Teredo)."""
    ip = ipaddress.ip_address(address.split("%")[0].strip("[]"))
    if isinstance(ip, ipaddress.IPv6Address):
        teredo = ip.teredo[1] if ip.teredo else None
        ip = ip.ipv4_mapped or ip.sixtofour or teredo or ip
    return not ip.is_global or ip.is_multicast


class PublicOnlyBackend(httpcore.AsyncNetworkBackend):
    """Opens TCP connections only to public addresses of a name, resolved at connect time."""

    def __init__(self, resolve: Resolver | None = None) -> None:
        self._inner = httpcore.AnyIOBackend()
        self._resolve = resolve or resolve_host

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: Iterable[httpcore.SOCKET_OPTION] | None = None,
    ) -> httpcore.AsyncNetworkStream:
        """Connect to the first public address that answers; refuse if any is not public."""
        try:
            addresses = [host] if is_ip(host) else await self._resolve(host)
        except OSError as exc:
            raise httpcore.ConnectError(f"cannot resolve {host}: {exc}") from exc
        if not addresses or any(is_local(address) for address in addresses):
            raise httpcore.ConnectError(f"{host} points to a local or private address")
        error: Exception | None = None
        for address in addresses:
            try:
                return await self._inner.connect_tcp(
                    address, port, timeout, local_address, socket_options
                )
            except httpcore.ConnectError as exc:
                error = exc
        raise error or httpcore.ConnectError(f"cannot connect to {host}")

    async def connect_unix_socket(
        self,
        path: str,
        timeout: float | None = None,
        socket_options: Iterable[httpcore.SOCKET_OPTION] | None = None,
    ) -> httpcore.AsyncNetworkStream:
        """Unix sockets are local by nature."""
        raise httpcore.ConnectError("Unix sockets are not fetched")

    async def sleep(self, seconds: float) -> None:
        """Wait (httpcore's retries use it)."""
        await self._inner.sleep(seconds)


def public_transport(resolve: Resolver | None = None) -> httpx.AsyncHTTPTransport:
    """An httpx transport whose direct connections go only to public addresses."""
    transport = httpx.AsyncHTTPTransport()
    # httpx has no switch for the network backend; its connection pool takes one
    transport._pool = httpcore.AsyncConnectionPool(
        ssl_context=httpx.create_ssl_context(), network_backend=PublicOnlyBackend(resolve)
    )
    return transport
