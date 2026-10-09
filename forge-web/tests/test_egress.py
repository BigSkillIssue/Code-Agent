"""The egress proxy: allow list, no private addresses, CONNECT and plain HTTP."""

import asyncio
from collections.abc import AsyncIterator

import pytest

from forge_web.egress import Denied, Egress, EgressPolicy, open_target, resolve


def test_allow_list_covers_subdomains_but_not_lookalikes() -> None:
    policy = EgressPolicy(["github.com", "*.npmjs.org"])
    assert policy.host_allowed("github.com") and policy.host_allowed("api.github.com")
    assert policy.host_allowed("registry.npmjs.org") and policy.host_allowed("npmjs.org")
    assert not policy.host_allowed("evilgithub.com") and not policy.host_allowed(
        "github.com.evil.io"
    )
    assert EgressPolicy(["*"]).host_allowed("anything.example")


@pytest.mark.parametrize(
    ("address", "allowed"),
    [("8.8.8.8", True), ("127.0.0.1", False), ("10.1.2.3", False), ("192.168.0.5", False),
     ("169.254.169.254", False), ("172.17.0.1", False), ("::1", False), ("fd00::1", False)],
)  # fmt: skip
def test_only_public_addresses(address: str, allowed: bool) -> None:
    assert EgressPolicy(["*"]).address_allowed(address) is allowed


async def test_a_name_that_resolves_to_loopback_is_refused() -> None:
    with pytest.raises(Denied, match="not public"):
        await resolve("localhost", 443, EgressPolicy(["*"]))


async def echo_server() -> tuple[asyncio.Server, int]:
    async def echo(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        while data := await reader.read(1024):
            writer.write(data)
            await writer.drain()
        writer.close()

    server = await asyncio.start_server(echo, "127.0.0.1", 0)
    return server, int(server.sockets[0].getsockname()[1])


@pytest.fixture
async def echo() -> AsyncIterator[int]:
    server, port = await echo_server()
    yield port
    server.close()


async def proxy_request(
    egress: Egress, head: str
) -> tuple[bytes, asyncio.StreamReader, asyncio.StreamWriter]:
    reader, writer = await egress.connect()
    writer.write(head.encode())
    await writer.drain()
    status = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 5)
    return status, reader, writer


async def test_connect_to_an_allowed_host(echo: int) -> None:
    egress = Egress(EgressPolicy(["127.0.0.1"], allow_private=True, ports={echo}))
    status, reader, writer = await proxy_request(
        egress, f"CONNECT 127.0.0.1:{echo} HTTP/1.1\r\n\r\n"
    )
    assert status.startswith(b"HTTP/1.1 200")
    writer.write(b"through the proxy")
    await writer.drain()
    assert await asyncio.wait_for(reader.read(100), 5) == b"through the proxy"
    writer.close()
    await egress.close()


@pytest.mark.parametrize(
    ("policy", "target"),
    [
        (EgressPolicy(["127.0.0.1"], allow_private=False), "127.0.0.1"),  # private address
        (EgressPolicy(["pypi.org"], allow_private=True), "127.0.0.1"),  # not on the list
    ],
)
async def test_refused_connections_get_403(echo: int, policy: EgressPolicy, target: str) -> None:
    policy.ports = {echo}
    status, reader, _writer = await proxy_request(
        Egress(policy), f"CONNECT {target}:{echo} HTTP/1.1\r\n\r\n"
    )
    assert status.startswith(b"HTTP/1.1 403")
    assert b"Forge Web blocked" in await reader.read(200)


async def test_ports_outside_the_list_are_refused(echo: int) -> None:
    egress = Egress(EgressPolicy(["127.0.0.1"], allow_private=True, ports={443}))
    status, _reader, _writer = await proxy_request(
        egress, f"CONNECT 127.0.0.1:{echo} HTTP/1.1\r\n\r\n"
    )
    assert status.startswith(b"HTTP/1.1 403")


async def test_plain_http_by_absolute_url() -> None:
    async def http(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        head = await reader.readuntil(b"\r\n\r\n")
        line = head.split(b"\r\n")[0].decode()
        writer.write(f"HTTP/1.1 200 OK\r\nContent-Length: {len(line)}\r\n\r\n{line}".encode())
        await writer.drain()
        writer.close()

    server = await asyncio.start_server(http, "127.0.0.1", 0)
    port = int(server.sockets[0].getsockname()[1])
    egress = Egress(EgressPolicy(["127.0.0.1"], allow_private=True, ports={port}))
    status, reader, _writer = await proxy_request(
        egress,
        f"GET http://127.0.0.1:{port}/simple/pkg/?x=1 HTTP/1.1\r\n"
        "Host: 127.0.0.1\r\nProxy-Authorization: x\r\n\r\n",
    )
    assert status.startswith(b"HTTP/1.1 200")
    assert await reader.read(100) == b"GET /simple/pkg/?x=1 HTTP/1.1"
    server.close()


async def test_the_servers_own_proxy_gets_only_names_it_cannot_resolve(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    asked: list[str] = []

    async def corporate_proxy(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        asked.append((await reader.readuntil(b"\r\n\r\n")).decode().split("\r\n")[0])
        writer.write(b"HTTP/1.1 200 Connection established\r\n\r\n")
        await writer.drain()
        writer.close()

    server = await asyncio.start_server(corporate_proxy, "127.0.0.1", 0)
    port = int(server.sockets[0].getsockname()[1])
    monkeypatch.setenv("HTTPS_PROXY", f"http://127.0.0.1:{port}")
    policy = EgressPolicy(["*"])
    try:
        with pytest.raises(Denied, match="not public"):  # the proxy might reach inside
            await open_target("localhost", 443, policy)
        _reader, writer = await open_target("no-such-name.invalid", 443, policy)
        writer.close()
    finally:
        server.close()
    assert asked == ["CONNECT no-such-name.invalid:443 HTTP/1.1"]
