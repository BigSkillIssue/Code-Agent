"""The browser's proxy (SEC-K6): it connects only where its policy allows, to the address it
checked, and forwards one plain HTTP request per connection."""

import asyncio

import pytest

from forge.local.browser_proxy import (
    GuardedProxy,
    browser_policy,
    origin_form,
    resolve_for_browser,
    split_target,
)


def test_the_policy_keeps_the_lan_closed_and_this_machine_behind_the_gate() -> None:
    gate = {"open": False}
    allow = browser_policy(lambda: gate["open"])
    assert allow("example.com", ["93.184.216.34"])
    refused = [
        ("router", ["192.168.1.1"]),
        ("10.0.0.5", ["10.0.0.5"]),
        ("metadata", ["169.254.169.254"]),
        ("rebound.test", ["127.0.0.1"]),
        ("mixed.test", ["93.184.216.34", "10.0.0.1"]),
        ("gone.test", []),
    ]
    for host, addresses in refused:
        assert not allow(host, addresses), host
    assert not allow("localhost", ["127.0.0.1"])  # the gate is shut
    gate["open"] = True
    assert allow("localhost", ["127.0.0.1", "::1"]) and allow("::1", ["::1"])
    assert not allow("localhost", ["10.0.0.7"])  # a hosts file pointing elsewhere
    assert not allow("rebound.test", ["127.0.0.1"])  # only names written as this machine


async def test_localhost_names_are_this_machine() -> None:
    assert await resolve_for_browser("shop.localhost") == ["127.0.0.1", "::1"]
    assert await resolve_for_browser("::1") == ["::1"]


def test_targets_and_the_forwarded_request() -> None:
    assert split_target("example.com:443", tunnel=True) == ("example.com", 443, "")
    assert split_target("[::1]:3000", tunnel=True) == ("::1", 3000, "")
    assert split_target("http://Shop.Localhost:5173/a?b=1", tunnel=False) == (
        "shop.localhost",
        5173,
        "/a?b=1",
    )
    assert split_target("http://example.com", tunnel=False) == ("example.com", 80, "/")
    with pytest.raises(ValueError):
        split_target("http://user@10.0.0.1/", tunnel=False)
    head = (
        b"GET http://example.com/x HTTP/1.1\r\nHost: example.com\r\n"
        b"Proxy-Connection: keep-alive\r\nConnection: keep-alive\r\nAccept: */*\r\n\r\n"
    )
    sent = origin_form(head, "GET", "/x", "HTTP/1.1")
    assert sent == (
        b"GET /x HTTP/1.1\r\nHost: example.com\r\nAccept: */*\r\nConnection: close\r\n\r\n"
    )
    upgrade = b"GET http://h/ws HTTP/1.1\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n\r\n"
    assert b"Connection: upgrade" in origin_form(upgrade, "GET", "/ws", "HTTP/1.1")


async def upstream() -> tuple[asyncio.Server, int, list[bytes]]:
    received: list[bytes] = []

    async def answer(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        received.append(await reader.readuntil(b"\r\n\r\n"))
        writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nok")
        await writer.drain()
        writer.close()

    server = await asyncio.start_server(answer, "127.0.0.1", 0)
    return server, server.sockets[0].getsockname()[1], received


async def ask(proxy_url: str, request: bytes) -> bytes:
    host, port = proxy_url.removeprefix("http://").split(":")
    reader, writer = await asyncio.open_connection(host, int(port))
    writer.write(request)
    await writer.drain()
    answer = await asyncio.wait_for(reader.read(), 5)
    writer.close()
    return answer


async def test_the_proxy_forwards_what_it_allows_and_refuses_the_rest() -> None:
    server, port, received = await upstream()
    checked: list[tuple[str, list[str]]] = []

    def allow(host: str, addresses: list[str]) -> bool:
        checked.append((host, addresses))
        return host == "app.test"

    async def resolve(host: str) -> list[str]:
        return ["127.0.0.1"]

    proxy = GuardedProxy(allow, resolve)
    url = await proxy.start()
    try:
        answer = await ask(
            url, f"GET http://app.test:{port}/a HTTP/1.1\r\nHost: x\r\n\r\n".encode()
        )
        assert answer.endswith(b"ok")
        assert received[0].startswith(b"GET /a HTTP/1.1\r\n")
        assert b"Connection: close" in received[0]
        assert await ask(url, f"CONNECT lan.test:{port} HTTP/1.1\r\n\r\n".encode()) == b""
        assert await ask(url, f"GET http://lan.test:{port}/ HTTP/1.1\r\n\r\n".encode()) == b""
        assert len(received) == 1  # nothing reached the server for the refused host
        assert proxy.take_refused() == ["lan.test", "lan.test"]
        assert proxy.take_refused() == []
        tunnel = await ask(
            url, f"CONNECT app.test:{port} HTTP/1.1\r\n\r\nGET /t HTTP/1.1\r\n\r\n".encode()
        )
        assert tunnel.startswith(b"HTTP/1.1 200 Connection Established") and tunnel.endswith(b"ok")
    finally:
        await proxy.close()
        server.close()
