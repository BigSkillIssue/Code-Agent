"""Live previews: each on its own host, opened with a one-time ticket, never seeing Forge's
cookies; HTTP and WebSocket pass through to a dev server in the project's sandbox."""

import asyncio
import json
import shlex
import socket
import sys
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx
import pytest
import pytest_asyncio
from websockets.asyncio.client import connect
from websockets.exceptions import InvalidStatus

from forge_web.db.models import ProjectMember
from forge_web.preview_auth import (
    COOKIE_NAME,
    PreviewAccess,
    PreviewBase,
    Target,
    preview_base,
    preview_target,
)
from forge_web.preview_headers import cross_site_refused, downstream_headers, upstream_headers
from forge_web.settings import load_settings
from support import LiveServer, Person, dev_settings, person

POSIX_ONLY = pytest.mark.skipif(sys.platform == "win32", reason="the test app runs in a shell")
APP = Path(__file__).parent / "preview_app.py"


# Hosts, tickets, cookies and headers (no server) -------------------------------------------


def settings_with(**overrides: Any) -> Any:
    return load_settings(Path("/nonexistent/forge-web.toml"), environ={}, overrides=overrides)


def test_preview_hosts() -> None:
    base = PreviewBase("https", "preview.example.net", None)
    found = preview_target(base, "P3000-abc123.Preview.Example.Net")
    assert found == Target("abc123", 3000, "https://p3000-abc123.preview.example.net")
    with_port = preview_target(PreviewBase("http", "localhost", 8420), "p80-x1.localhost:8420")
    assert with_port == Target("x1", 80, "http://p80-x1.localhost:8420")
    for bad in (None, "", "preview.example.net", "p0-abc.preview.example.net",
                "p70000-abc.preview.example.net", "x3000-abc.preview.example.net",
                "p3000-abc.evil.net", "p3000-abc.preview.example.net.evil.com",
                "a.p3000-abc.preview.example.net", "p3000-ab_c.preview.example.net",
                "p3000-.preview.example.net"):  # fmt: skip
        assert preview_target(base, bad) is None, bad
    assert base.url("abc123", 3000, "/x?y=1") == "https://p3000-abc123.preview.example.net/x?y=1"
    assert base.frame_source() == "https://*.preview.example.net"


def test_where_previews_live() -> None:
    local = preview_base(settings_with(**{"server.port": 8420}))
    assert local == PreviewBase("http", "localhost", 8420)  # single-port mode on loopback
    assert preview_base(settings_with(**{"server.host": "0.0.0.0"})) is None  # no domain: off
    public = settings_with(**{"server.host": "0.0.0.0", "preview.domain": "preview.example.net",
                              "server.public_url": "https://forge.example.com"})  # fmt: skip
    assert preview_base(public) == PreviewBase("https", "preview.example.net", None)
    plain = settings_with(**{"preview.domain": "previews.lan", "preview.https": False,
                             "preview.port": 8080})  # fmt: skip
    assert preview_base(plain) == PreviewBase("http", "previews.lan", 8080)


def test_tickets_are_single_use_and_cookies_are_bound() -> None:
    access = PreviewAccess(b"k" * 32)
    target = Target("proj1", 3000, "http://p3000-proj1.localhost:8420")
    ticket = access.issue("u1", "s1", "proj1", 3000, now=1000.0)
    other = Target("proj2", 3000, "http://p3000-proj2.localhost:8420")
    assert access.redeem(ticket, other, now=1001.0) is None  # wrong project: and now it is used
    assert access.redeem(ticket, target, now=1001.0) is None
    fresh = access.issue("u1", "s1", "proj1", 3000, now=1000.0)
    assert access.redeem(fresh, target, now=1061.0) is None  # expired after a minute
    good = access.issue("u1", "s1", "proj1", 3000, now=1000.0)
    grant = access.redeem(good, target, now=1010.0)
    assert grant is not None and grant.user_id == "u1" and grant.session_id == "s1"
    value = access.cookie_value(grant)
    assert access.read_cookie(value, target, now=1010.0) == grant
    assert access.read_cookie(value, other, now=1010.0) is None
    assert access.read_cookie(value, Target("proj1", 3001, "x"), now=1010.0) is None
    assert access.read_cookie(value, target, now=grant.expires + 1.0) is None
    tampered = value.replace("u1", "u2", 1)
    assert access.read_cookie(tampered, target, now=1010.0) is None
    assert PreviewAccess(b"z" * 32).read_cookie(value, target, now=1010.0) is None


def test_headers_to_the_app_and_back() -> None:
    target = Target("proj1", 5173, "https://p5173-proj1.preview.example.net")
    sent = [
        (b"host", b"p5173-proj1.preview.example.net"),
        (b"origin", b"https://p5173-proj1.preview.example.net"),
        (b"referer", b"https://p5173-proj1.preview.example.net/page?q=1"),
        (b"cookie", f"theme=dark; {COOKIE_NAME}=v; __Host-forge_session=s; forge_csrf=c".encode()),
        (b"x-forwarded-for", b"10.0.0.1"), (b"connection", b"keep-alive"),
        (b"accept", b"text/html"),
    ]  # fmt: skip
    up = dict(upstream_headers(sent, target))
    assert up[b"host"] == b"localhost:5173" and up[b"origin"] == b"http://localhost:5173"
    assert up[b"referer"] == b"http://localhost:5173/page?q=1"
    assert up[b"cookie"] == b"theme=dark" and up[b"accept"] == b"text/html"
    assert b"x-forwarded-for" not in up and b"connection" not in up
    foreign = dict(upstream_headers([(b"origin", b"https://evil.example")], target))
    assert b"origin" not in foreign
    only_forge = dict(upstream_headers([(b"cookie", f"{COOKIE_NAME}=v".encode())], target))
    assert b"cookie" not in only_forge
    back = downstream_headers(
        [(b"set-cookie", b"a=1; Path=/; Domain=.preview.example.net; HttpOnly"),
         (b"set-cookie", b"forge_session=x; Path=/"), (b"location", b"http://localhost:5173/next"),
         (b"transfer-encoding", b"chunked"), (b"content-type", b"text/html")],
        5173, "https://forge.example.com",
    )  # fmt: skip
    assert (b"set-cookie", b"a=1; Path=/; HttpOnly") in back
    assert not [v for n, v in back if n == b"set-cookie" and v.startswith(b"forge_session")]
    assert (b"location", b"/next") in back and b"transfer-encoding" not in dict(back)
    assert (b"content-security-policy", b"frame-ancestors https://forge.example.com") in back


def test_cross_site_requests_are_only_navigations() -> None:
    def headers(site: str, mode: str) -> list[tuple[bytes, bytes]]:
        return [(b"sec-fetch-site", site.encode()), (b"sec-fetch-mode", mode.encode())]

    assert not cross_site_refused("GET", [])  # an old browser that sends no fetch metadata
    assert not cross_site_refused("POST", headers("same-origin", "cors"))
    assert not cross_site_refused("GET", headers("cross-site", "navigate"))  # Forge's iframe
    assert cross_site_refused("POST", headers("cross-site", "navigate"))
    assert cross_site_refused("GET", headers("cross-site", "cors"))
    assert cross_site_refused("GET", headers("same-site", "no-cors"))  # another preview


# Through a real server and sandbox ---------------------------------------------------------


@dataclass
class World:
    """A server, its admin's project A running the test app, project B, a viewer, an outsider."""

    server: LiveServer
    client: httpx.AsyncClient
    project_a: str
    project_b: str
    app_port: int
    viewer: Person
    outsider: Person

    def host(self, project_id: str, port: int | None = None) -> str:
        server_port = urlsplit(self.server.url).port
        return f"p{port or self.app_port}-{project_id}.localhost:{server_port}"


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


async def wait_for_port(client: httpx.AsyncClient, project_id: str, port: int) -> None:
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        found = (await client.get(f"/api/projects/{project_id}/preview")).json()
        if port in [p["port"] for p in found["ports"]]:
            return
        await asyncio.sleep(0.2)
    raise AssertionError(f"nothing listens on {port}")


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def world(tmp_path_factory: pytest.TempPathFactory) -> AsyncIterator[World]:
    with LiveServer(dev_settings(tmp_path_factory.mktemp("preview") / "data")) as server:
        server.services.settings.preview.port = urlsplit(server.url).port
        async with httpx.AsyncClient(base_url=server.url, headers=server.headers(),
                                     timeout=60) as client:  # fmt: skip
            a = (await client.post("/api/projects", json={"name": "A"})).json()["id"]
            b = (await client.post("/api/projects", json={"name": "B"})).json()["id"]
            port = free_port()
            command = f"{shlex.quote(sys.executable)} {shlex.quote(str(APP))} {port}"
            started = await client.post(f"/api/projects/{a}/preview/programs",
                                        json={"command": command})  # fmt: skip
            assert started.status_code == 201, started.text
            await wait_for_port(client, a, port)
            viewer, outsider = await person(server, "viewer"), await person(server, "outsider")
            async with server.services.db.session() as session, session.begin():
                session.add(ProjectMember(project_id=a, user_id=viewer.id, role="viewer"))
            yield World(server, client, a, b, port, viewer, outsider)
            await client.delete(f"/api/projects/{a}/preview/programs/{started.json()['id']}")
            for who in (viewer, outsider):
                await who.web.client.aclose()


on_shared_loop = pytest.mark.asyncio(loop_scope="module")


async def enter(world: World, web: Any, project_id: str, port: int | None = None) -> str:
    """Open the preview like the UI does; the preview cookie's value."""
    opened = await web.post(f"/api/projects/{project_id}/preview/{port or world.app_port}/open")
    assert opened.status_code == 200, opened.text
    url = urlsplit(opened.json()["url"])
    assert url.hostname is not None and url.hostname.endswith(".localhost")
    async with httpx.AsyncClient(base_url=world.server.url) as browser:
        entered = await browser.get(f"{url.path}?{url.query}", headers={"Host": url.netloc})
    assert entered.status_code == 303, entered.text
    assert entered.headers["location"] == "/"
    cookie = entered.headers["set-cookie"]
    for part in ("HttpOnly", "Secure", "SameSite=None", "Path=/", "Partitioned"):
        assert part in cookie, part
    assert "domain=" not in cookie.lower()
    return cookie.split(";")[0].split("=", 1)[1]


async def preview_get(world: World, path: str, *, host: str, cookies: str = "",
                      **headers: str) -> httpx.Response:  # fmt: skip
    async with httpx.AsyncClient(base_url=world.server.url, timeout=30) as browser:
        sent = {"Host": host, **({"Cookie": cookies} if cookies else {}), **headers}
        return await browser.get(path, headers=sent)


@POSIX_ONLY
@on_shared_loop
async def test_a_preview_needs_a_ticket(world: World) -> None:
    host = world.host(world.project_a)
    refused = await preview_get(world, "/echo", host=host)
    assert refused.status_code == 401 and "Forge" in refused.text
    opened = await world.client.post(
        f"/api/projects/{world.project_a}/preview/{world.app_port}/open"
    )
    url = urlsplit(opened.json()["url"])
    async with httpx.AsyncClient(base_url=world.server.url) as browser:
        first = await browser.get(f"{url.path}?{url.query}", headers={"Host": url.netloc})
        again = await browser.get(f"{url.path}?{url.query}", headers={"Host": url.netloc})
    assert first.status_code == 303 and again.status_code == 401  # a ticket works once
    cookie = first.headers["set-cookie"].split(";")[0]
    assert (await preview_get(world, "/echo", host=host, cookies=cookie)).status_code == 200


@POSIX_ONLY
@on_shared_loop
async def test_the_app_never_sees_forge_cookies(world: World) -> None:
    value = await enter(world, world.client, world.project_a)
    cookies = f"theme=dark; {COOKIE_NAME}={value}; {world.server.cookie}"
    host = world.host(world.project_a)
    seen = await preview_get(world, "/echo?x=1", host=host, cookies=cookies,
                             **{"X-Forwarded-For": "10.0.0.9"})  # fmt: skip
    assert seen.status_code == 200, seen.text
    data = seen.json()
    assert data["host"] == f"localhost:{world.app_port}" and data["query"] == "x=1"
    assert data["cookie"] == "theme=dark" and data["forwarded"] is None
    set_cookies = seen.headers.get_list("set-cookie")
    assert "app=1; Path=/" in set_cookies and "plain=2; Path=/; HttpOnly" in set_cookies
    assert not [c for c in set_cookies if "domain" in c.lower() or c.startswith("forge_")]
    assert "frame-ancestors" in seen.headers["content-security-policy"]
    moved = await preview_get(world, "/moved", host=host, cookies=cookies)
    assert moved.status_code == 302 and moved.headers["location"] == "/echo?from=moved"
    big = await preview_get(world, "/big", host=host, cookies=cookies)
    assert big.status_code == 200 and len(big.content) == 64 * 16384
    async with httpx.AsyncClient(base_url=world.server.url, timeout=30) as browser:
        posted = await browser.post("/echo", content=b"hello body",
                                    headers={"Host": host, "Cookie": cookies})  # fmt: skip
    assert posted.json()["body"] == "hello body" and posted.json()["method"] == "POST"


@POSIX_ONLY
@on_shared_loop
async def test_previews_are_separate(world: World) -> None:
    value = await enter(world, world.client, world.project_a)
    on_b = await preview_get(world, "/echo", host=world.host(world.project_b),
                             cookies=f"{COOKIE_NAME}={value}")  # fmt: skip
    assert on_b.status_code == 401  # project A's cookie does not open project B
    other_port = await preview_get(world, "/echo", host=world.host(world.project_a, 1),
                                   cookies=f"{COOKIE_NAME}={value}")  # fmt: skip
    assert other_port.status_code == 401  # nor another port of A
    opened = await world.client.post(
        f"/api/projects/{world.project_a}/preview/{world.app_port}/open"
    )
    url = urlsplit(opened.json()["url"])
    async with httpx.AsyncClient(base_url=world.server.url) as browser:
        wrong = await browser.get(f"{url.path}?{url.query}",
                                  headers={"Host": world.host(world.project_b)})  # fmt: skip
    assert wrong.status_code == 401  # A's ticket on B's host
    path = f"/api/projects/{world.project_a}/preview/{world.app_port}/open"
    assert (await world.outsider.web.post(path)).status_code == 404


@POSIX_ONLY
@on_shared_loop
async def test_viewers_may_look_but_not_start_and_lose_access_with_membership(
    world: World,
) -> None:
    value = await enter(world, world.viewer.web, world.project_a)
    host, cookie = world.host(world.project_a), f"{COOKIE_NAME}={value}"
    assert (await preview_get(world, "/echo", host=host, cookies=cookie)).status_code == 200
    start = await world.viewer.web.post(f"/api/projects/{world.project_a}/preview/programs",
                                        {"command": "true"})  # fmt: skip
    assert start.status_code == 403
    async with world.server.services.db.session() as session, session.begin():
        member = await session.get(ProjectMember, (world.project_a, world.viewer.id))
        await session.delete(member)
    world.server.services.previews.forget_checks()  # as if the 30 s check interval passed
    assert (await preview_get(world, "/echo", host=host, cookies=cookie)).status_code == 401
    async with world.server.services.db.session() as session, session.begin():
        session.add(ProjectMember(project_id=world.project_a, user_id=world.viewer.id,
                                  role="viewer"))  # fmt: skip


@POSIX_ONLY
@on_shared_loop
async def test_other_sites_may_only_navigate(world: World) -> None:
    value = await enter(world, world.client, world.project_a)
    host, cookie = world.host(world.project_a), f"{COOKIE_NAME}={value}"
    fetched = await preview_get(
        world,
        "/echo",
        host=host,
        cookies=cookie,
        **{"Sec-Fetch-Site": "cross-site", "Sec-Fetch-Mode": "cors"},
    )
    assert fetched.status_code == 403
    navigated = await preview_get(
        world,
        "/echo",
        host=host,
        cookies=cookie,
        **{"Sec-Fetch-Site": "cross-site", "Sec-Fetch-Mode": "navigate"},
    )
    assert navigated.status_code == 200
    # A preview cannot use Forge's API as the user, even with the session cookie in hand.
    sneaky = await world.client.post(
        "/api/projects", json={"name": "From a preview"}, headers={"Origin": f"http://{host}"}
    )
    assert sneaky.status_code == 403
    page = await world.client.get("/")
    csp = page.headers["content-security-policy"]
    assert "frame-ancestors 'none'" in csp and "frame-src http://*.localhost:" in csp


@POSIX_ONLY
@on_shared_loop
async def test_websockets_pass_through(world: World) -> None:
    value = await enter(world, world.client, world.project_a)
    host = world.host(world.project_a)
    server_port = urlsplit(world.server.url).port
    headers = {"Cookie": f"{COOKIE_NAME}={value}; {world.server.cookie}",
               "Origin": f"http://{host}"}  # fmt: skip
    url = f"ws://{host}/ws"
    async with connect(
        url,
        host="127.0.0.1",
        port=server_port,
        additional_headers=headers,
        subprotocols=["vite-hmr"],
        max_size=None,
    ) as ws:
        assert ws.subprotocol == "vite-hmr"
        hello = json.loads(await asyncio.wait_for(ws.recv(), 15))
        assert hello["origin"] == f"http://localhost:{world.app_port}"
        assert hello["host"] == f"localhost:{world.app_port}"
        assert hello["cookie"] == ""  # Forge's cookies stay out
        await ws.send("ping")
        assert await asyncio.wait_for(ws.recv(), 15) == "echo:ping"
        payload = bytes(range(256)) * 8192  # 2 MiB
        await ws.send(payload)
        assert await asyncio.wait_for(ws.recv(), 30) == payload[::-1]
    for bad in ({**headers, "Origin": "https://evil.example"}, {"Origin": f"http://{host}"}):
        with pytest.raises(InvalidStatus):
            await connect(url, host="127.0.0.1", port=server_port, additional_headers=bad)


@POSIX_ONLY
@on_shared_loop
async def test_nothing_listening_and_the_overview(world: World) -> None:
    unused = free_port()
    value = await enter(world, world.client, world.project_a, unused)
    missing = await preview_get(world, "/", host=world.host(world.project_a, unused),
                                cookies=f"{COOKIE_NAME}={value}")  # fmt: skip
    assert missing.status_code == 502 and str(unused) in missing.text
    b = world.project_b
    package = '{"scripts": {"dev": "vite", "start": "node server.js", "test": "vitest"}}'
    for path, text in (("package.json", package), ("index.html", "<h1>hi</h1>")):
        saved = await world.client.put(f"/api/projects/{b}/files/content",
                                       json={"path": path, "text": text})  # fmt: skip
        assert saved.status_code == 200, saved.text
    overview = (await world.client.get(f"/api/projects/{b}/preview")).json()
    commands = [s["command"] for s in overview["suggestions"]]
    assert commands[:2] == ["npm install && npm run dev", "npm install && npm start"]
    assert "python3 -m http.server 8000 --bind 127.0.0.1" in commands
    assert overview["enabled"] is True
    a = (await world.client.get(f"/api/projects/{world.project_a}/preview")).json()
    program = next(p for p in a["programs"] if p["running"])
    output = await world.client.get(f"/api/projects/{world.project_a}/preview/programs/"
                                    f"{program['id']}/output")  # fmt: skip
    assert output.status_code == 200 and "lines" in output.json()
    server_port = urlsplit(world.server.url).port
    assert server_port not in [p["port"] for p in a["ports"]]  # Forge itself is not offered


@POSIX_ONLY
@on_shared_loop
async def test_previews_are_off_on_a_public_server_without_a_domain(world: World) -> None:
    server = world.server.services.settings.server
    server.host = "0.0.0.0"
    try:
        overview = (await world.client.get(f"/api/projects/{world.project_a}/preview")).json()
        assert overview["enabled"] is False
        opened = await world.client.post(
            f"/api/projects/{world.project_a}/preview/{world.app_port}/open"
        )
        assert opened.status_code == 409
        health = await preview_get(world, "/api/health", host=world.host(world.project_a))
        assert health.status_code == 200 and health.json()["ok"]  # just Forge, not a preview
    finally:
        server.host = "127.0.0.1"
