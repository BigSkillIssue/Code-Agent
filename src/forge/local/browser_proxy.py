"""A forward proxy for the browser: every connection Chromium opens goes through it, and it
connects only where a policy allows, to the address it resolved and checked itself.

Playwright's request routing sees only the first URL of a redirect chain, and Chromium resolves
a name again after a check (DNS rebinding). Here each hop of a redirect is a connection of its
own, and the address checked is the address used. Plain HTTP requests are forwarded one per
connection (`Connection: close`), so a kept-alive connection cannot carry a request to another
host past the check.
"""

import asyncio
import contextlib
import ipaddress
from collections.abc import Awaitable, Callable

from forge.runtime.web import is_ip, is_local, is_loopback_host, resolve_host

Policy = Callable[[str, list[str]], bool]  # host and its addresses -> may connect
Resolver = Callable[[str], Awaitable[list[str]]]

MAX_HEAD = 64 * 1024
CONNECT_TIMEOUT_S = 10.0
HOP_HEADERS = (b"proxy-connection", b"proxy-authorization", b"connection", b"keep-alive")


def browser_policy(loopback: Callable[[], bool]) -> Policy:
    """Public addresses always; this machine's own servers only while `loopback()` is true
    and only when the URL names them as such (localhost, 127.x, ::1); never the LAN."""

    def allow(host: str, addresses: list[str]) -> bool:
        if not addresses:
            return False
        if is_loopback_host(host):
            return loopback() and all(ipaddress.ip_address(a).is_loopback for a in addresses)
        return not any(is_local(address) for address in addresses)

    return allow


async def resolve_for_browser(host: str) -> list[str]:
    """Addresses of `host`; `localhost` and `*.localhost` are this machine, as in Chromium."""
    if host == "localhost" or host.endswith(".localhost"):
        return ["127.0.0.1", "::1"]
    return [host.strip("[]")] if is_ip(host) else await resolve_host(host)


class GuardedProxy:
    """An HTTP proxy on 127.0.0.1 that refuses connections its policy does not allow."""

    def __init__(self, allow: Policy, resolve: Resolver | None = None) -> None:
        self.allow = allow
        self.resolve = resolve or resolve_for_browser
        self.refused: list[str] = []  # hosts refused since the last `take_refused`
        self._server: asyncio.Server | None = None

    async def start(self) -> str:
        """Start listening; returns the proxy's URL for the browser."""
        self._server = await asyncio.start_server(self._serve, "127.0.0.1", 0, limit=MAX_HEAD)
        port = self._server.sockets[0].getsockname()[1]
        return f"http://127.0.0.1:{port}"

    async def close(self) -> None:
        """Stop listening (open tunnels end with the browser)."""
        if self._server is not None:
            self._server.close()
            self._server = None

    def take_refused(self) -> list[str]:
        """The hosts refused since the last call."""
        refused, self.refused = self.refused, []
        return refused

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), CONNECT_TIMEOUT_S)
            await self._forward(head, reader, writer)
        except (OSError, ValueError, asyncio.IncompleteReadError, asyncio.LimitOverrunError):
            pass  # a broken or refused request just ends; the browser reports it
        finally:
            with contextlib.suppress(Exception):
                writer.close()

    async def _forward(
        self, head: bytes, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        method, target, version = head.split(b"\r\n", 1)[0].decode("latin-1").split(" ")
        tunnel = method == "CONNECT"
        host, port, path = split_target(target, tunnel)
        upstream = await self._connect(host, port)
        if upstream is None:
            return
        up_reader, up_writer = upstream
        if tunnel:
            writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
        else:
            up_writer.write(origin_form(head, method, path, version))
        await asyncio.gather(pipe(reader, up_writer), pipe(up_reader, writer))

    async def _connect(
        self, host: str, port: int
    ) -> tuple[asyncio.StreamReader, asyncio.StreamWriter] | None:
        """Resolve, check, connect to the first address that answers; None when refused."""
        try:
            addresses = await self.resolve(host)
        except OSError:
            addresses = []
        if not self.allow(host, addresses):
            self.refused.append(host)
            return None
        for address in addresses:
            with contextlib.suppress(OSError):  # TimeoutError is an OSError
                return await asyncio.wait_for(
                    asyncio.open_connection(address, port), CONNECT_TIMEOUT_S
                )
        return None


def split_target(target: str, tunnel: bool) -> tuple[str, int, str]:
    """Host, port and path of a proxy request target (`host:port` or `http://host:port/path`)."""
    if tunnel:
        authority, path, default = target, "", 443
    elif target.startswith("http://"):
        authority, _, rest = target[len("http://") :].partition("/")
        path, default = "/" + rest, 80
    else:
        raise ValueError(f"not a proxy request: {target}")
    if "@" in authority:
        raise ValueError("credentials in a proxy target")
    if authority.startswith("["):  # [IPv6]:port
        host, _, rest = authority[1:].partition("]")
        port = rest.removeprefix(":")
    else:
        host, _, port = authority.partition(":")
    if not host:
        raise ValueError(f"no host in {target}")
    return host.lower(), int(port or default), path


def origin_form(head: bytes, method: str, path: str, version: str) -> bytes:
    """The request as the server expects it: `GET /path`, without the proxy's headers, and
    closed after one response unless it is a protocol upgrade (WebSocket)."""
    lines = head.rstrip(b"\r\n").split(b"\r\n")[1:]
    upgrade = any(line.lower().startswith(b"upgrade:") for line in lines)
    kept = [line for line in lines if line.split(b":", 1)[0].strip().lower() not in HOP_HEADERS]
    kept.append(b"Connection: upgrade" if upgrade else b"Connection: close")
    first = f"{method} {path} {version}".encode("latin-1")
    return b"\r\n".join([first, *kept]) + b"\r\n\r\n"


async def pipe(source: asyncio.StreamReader, sink: asyncio.StreamWriter) -> None:
    """Copy bytes until either side ends, then close the sink."""
    try:
        while data := await source.read(65536):
            sink.write(data)
            await sink.drain()
    except OSError:
        pass
    finally:
        with contextlib.suppress(Exception):
            sink.close()
