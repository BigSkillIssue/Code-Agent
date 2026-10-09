"""The egress proxy: the only way programs in a container (which has no network) reach the
internet, and only to hosts on the allow list.

It speaks just enough HTTP proxy: `CONNECT host:443` for HTTPS (most package managers) and
absolute-URL requests for plain HTTP. A host must be on the allow list, and every address it
resolves to must be public — never loopback, private, link-local (cloud metadata) or the server's
own network — and the connection goes to exactly the address that was checked. If the server
itself needs a proxy (HTTPS_PROXY), CONNECT requests are passed on through it.
"""

import asyncio
import contextlib
import ipaddress
import os
import socket
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from forge_sandbox.streams import CHUNK

HEAD_LIMIT = 16 * 1024
HEAD_TIMEOUT = 30.0


@dataclass
class EgressPolicy:
    """Which hosts and ports may be reached."""

    allow: list[str]
    allow_private: bool = False
    ports: set[int] = field(default_factory=lambda: {80, 443})

    def host_allowed(self, host: str) -> bool:
        """An entry allows the host and its subdomains; "*" allows every host."""
        if "*" in self.allow:
            return True
        host = host.lower().rstrip(".")
        for entry in self.allow:
            entry = entry.lower().strip().removeprefix("*.").rstrip(".")
            if entry and (host == entry or host.endswith("." + entry)):
                return True
        return False

    def address_allowed(self, address: str) -> bool:
        """Public addresses only (unless private ones are allowed, for tests)."""
        ip = ipaddress.ip_address(address)
        return self.allow_private or ip.is_global


class Denied(Exception):
    """A connection the policy forbids."""


class Unresolved(Denied):
    """A name this server cannot resolve itself (its own proxy may)."""


async def resolve(host: str, port: int, policy: EgressPolicy) -> str:
    """One checked address of `host`; Denied if any address is not allowed."""
    try:
        infos = await asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except OSError:
        raise Unresolved(f"{host} cannot be resolved") from None
    addresses = [str(info[4][0]) for info in infos]
    if not addresses or not all(policy.address_allowed(a) for a in addresses):
        raise Denied(f"{host} resolves to an address that is not public")
    return addresses[0]


def upstream_proxy() -> tuple[str, int] | None:
    """The server's own HTTPS proxy, if it has one."""
    value = os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy")
    if not value:
        return None
    parts = urlsplit(value if "://" in value else f"http://{value}")
    return (parts.hostname, parts.port or 3128) if parts.hostname else None


async def read_head(reader: asyncio.StreamReader) -> list[str]:
    """The request line and headers."""
    raw = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), HEAD_TIMEOUT)
    if len(raw) > HEAD_LIMIT:
        raise Denied("request head too large")
    return raw.decode("latin-1").split("\r\n")[:-2]


async def open_target(
    host: str, port: int, policy: EgressPolicy
) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    """A connection to an allowed host (through the server's proxy when it has one)."""
    if not policy.host_allowed(host):
        raise Denied(f"{host} is not on the allow list")
    if port not in policy.ports:
        raise Denied(f"port {port} is not allowed")
    chained = upstream_proxy() if port == 443 else None
    try:
        address = await resolve(host, port, policy)
    except Unresolved:
        # Only a name this server cannot look up goes to its proxy; one that resolves to a
        # private address never does (the server's proxy may well reach inside).
        if chained is None:
            raise
        address = ""
    if chained is None:
        return await asyncio.open_connection(address, port)
    reader, writer = await asyncio.open_connection(*chained)
    writer.write(f"CONNECT {host}:{port} HTTP/1.1\r\nHost: {host}:{port}\r\n\r\n".encode())
    await writer.drain()
    status = (await read_head(reader))[0]
    if " 200" not in status:
        writer.close()
        raise Denied(f"the server's proxy refused {host}")
    return reader, writer


async def serve(
    reader: asyncio.StreamReader, writer: asyncio.StreamWriter, policy: EgressPolicy
) -> None:
    """Handle one proxy connection."""
    try:
        head = await read_head(reader)
        method, target, _version = head[0].split(" ", 2)
        if method.upper() == "CONNECT":
            host, _, port = target.rpartition(":")
            remote_reader, remote_writer = await open_target(host.strip("[]"), int(port), policy)
            writer.write(b"HTTP/1.1 200 Connection established\r\n\r\n")
            await writer.drain()
        else:
            remote_reader, remote_writer = await plain_http(method, target, head[1:], policy)
    except (Denied, ValueError, OSError, asyncio.IncompleteReadError, TimeoutError) as err:
        message = str(err) or "not allowed"
        writer.write(
            f"HTTP/1.1 403 Forbidden\r\nContent-Type: text/plain\r\nConnection: close\r\n\r\n"
            f"Forge Web blocked this connection: {message}\n".encode()
        )
        with contextlib.suppress(Exception):
            await writer.drain()
        writer.close()
        return
    await _splice(reader, writer, remote_reader, remote_writer)


async def plain_http(
    method: str, target: str, headers: list[str], policy: EgressPolicy
) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    """Forward one plain-HTTP request given as an absolute URL."""
    url = urlsplit(target)
    if url.scheme != "http" or not url.hostname:
        raise Denied("only absolute http:// URLs can go through the proxy without CONNECT")
    remote_reader, remote_writer = await open_target(url.hostname, url.port or 80, policy)
    path = url.path or "/"
    if url.query:
        path += "?" + url.query
    kept = [h for h in headers if not h.lower().startswith(("proxy-", "connection:"))]
    request = "\r\n".join([f"{method} {path} HTTP/1.1", *kept, "Connection: close", "", ""])
    remote_writer.write(request.encode("latin-1"))
    await remote_writer.drain()
    return remote_reader, remote_writer


async def _splice(
    a_reader: asyncio.StreamReader,
    a_writer: asyncio.StreamWriter,
    b_reader: asyncio.StreamReader,
    b_writer: asyncio.StreamWriter,
) -> None:
    async def copy(source: asyncio.StreamReader, sink: asyncio.StreamWriter) -> None:
        with contextlib.suppress(OSError, ConnectionError):
            while data := await source.read(CHUNK):
                sink.write(data)
                await sink.drain()
        with contextlib.suppress(Exception):
            sink.close()

    await asyncio.gather(copy(a_reader, b_writer), copy(b_reader, a_writer))


class Egress:
    """Connections into the egress proxy for sandboxes, without a listening port."""

    def __init__(self, policy: EgressPolicy) -> None:
        self.policy = policy
        self._tasks: set[asyncio.Task[None]] = set()

    async def connect(self) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        """One end of a fresh connection; the proxy serves the other end."""
        ours, theirs = socket.socketpair()
        reader, writer = await asyncio.open_connection(sock=ours)
        proxy_reader, proxy_writer = await asyncio.open_connection(sock=theirs)
        task = asyncio.create_task(serve(proxy_reader, proxy_writer, self.policy))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return reader, writer

    async def close(self) -> None:
        """Stop every open connection."""
        for task in self._tasks:
            task.cancel()
