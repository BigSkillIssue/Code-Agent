"""Agents test what they build in a browser (S78): the lead drives one, and it may open the
product's servers on this machine; other local addresses stay closed, and the browser agent
researching the web still reaches only public pages."""

import os
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

from forge.config import BrowserConfig, ForgeConfig
from forge.ctx import Ctx
from forge.local.playwright_browser import PlaywrightBrowsers
from forge.ports import BrowserError
from forge.providers.base import ToolCall, ToolResult
from forge.runtime import web
from forge.runtime.web import is_loopback_host
from forge.tools import agent_tools, call_tool
from support import make_ctx
from test_browser import FakeBrowsers

BROWSER_TOOLS = {"browser_open", "browser_click", "browser_type", "browser_scroll",
                 "browser_back", "browser_screenshot", "browser_read"}  # fmt: skip


@pytest.fixture(autouse=True)
def public_dns(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_resolve(host: str) -> list[str]:
        return ["10.1.2.3"] if "internal" in host else ["93.184.216.34"]

    monkeypatch.setattr(web, "resolve_host", fake_resolve)


def lead(root: Path, **cfg: Any) -> tuple[Ctx, FakeBrowsers, FakeBrowsers]:
    ctx = make_ctx(root, cfg=ForgeConfig(**cfg))
    research, testing = FakeBrowsers(), FakeBrowsers()
    ctx.state.browser_factory, ctx.state.test_browsers = research, testing
    return ctx, research, testing


async def run(ctx: Ctx, name: str, **arguments: Any) -> ToolResult:
    return await call_tool(ctx, ToolCall(id="c1", name=name, arguments=arguments))


def names(ctx: Ctx, role: str) -> set[str]:
    return {t.name for t in agent_tools(ctx, role)}


def test_the_lead_gets_the_browser_tools(tmp_project: Path) -> None:
    ctx, _, _ = lead(tmp_project)
    assert names(ctx, "coder") >= BROWSER_TOOLS
    assert not BROWSER_TOOLS & names(ctx, "planner")  # read-only roles test nothing
    sub = make_ctx(tmp_project, agent_id="a1", role="coder")
    sub.state = ctx.state
    assert not BROWSER_TOOLS & names(sub, "coder")  # sub-agents: only when the lead gives them
    off, _, _ = lead(tmp_project, browser=BrowserConfig(agents=False))
    assert not BROWSER_TOOLS & names(off, "coder")
    without = make_ctx(tmp_project)
    assert not BROWSER_TOOLS & names(without, "coder")  # no browser in this session


async def test_the_lead_opens_its_product_on_this_machine(tmp_project: Path) -> None:
    ctx, research, testing = lead(tmp_project)
    for url in ("http://localhost:5173/orders", "http://127.0.0.1:8000/health",
                "http://[::1]:3000/", "http://shop.localhost:5173/"):  # fmt: skip
        result = await run(ctx, "browser_open", url=url)
        assert result.ok, (url, result.text)
    assert [a[1] for a in testing.made[0].actions] == [
        "http://localhost:5173/orders",  # kept as http: a dev server has no TLS
        "http://127.0.0.1:8000/health",
        "http://[::1]:3000/",
        "http://shop.localhost:5173/",
    ]
    assert research.made == []  # the lead's browser is the testing one


async def test_other_local_addresses_stay_closed(tmp_project: Path) -> None:
    ctx, _, testing = lead(tmp_project)
    for url in ("http://192.168.1.20/", "http://10.0.0.1:8080/", "https://db.internal/",
                "http://router/", "file:///etc/passwd"):  # fmt: skip
        result = await run(ctx, "browser_open", url=url)
        assert result.code == "invalid_args", url
    off, _, _ = lead(tmp_project, browser=BrowserConfig(loopback=False))
    assert (await run(off, "browser_open", url="http://localhost:5173/")).code == "invalid_args"
    assert not testing.made or not testing.made[0].actions


async def test_the_research_browser_stays_on_public_pages(tmp_project: Path) -> None:
    ctx, research, testing = lead(tmp_project)
    agent = make_ctx(tmp_project, cfg=ctx.cfg, agent_id="a1", role="browser")
    agent.state = ctx.state
    assert (await run(agent, "browser_open", url="http://localhost:5173/")).code == "invalid_args"
    assert (await run(agent, "browser_open", url="https://example.com/")).ok
    assert len(research.made) == 1 and testing.made == []


def test_loopback_means_written_as_this_machine() -> None:
    for host in ("localhost", "LOCALHOST", "app.localhost", "127.0.0.1", "127.8.9.1", "::1",
                 "[::1]"):  # fmt: skip
        assert is_loopback_host(host), host
    for host in ("10.0.0.1", "192.168.0.2", "example.com", "localhost.example.com", "", "0.0.0.0"):
        assert not is_loopback_host(host), host


# ------------------------------------------------------------------ the real browser


class Hello(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        data = b"<title>Orders</title><h1>Your orders</h1>"
        self.send_response(200)
        self.send_header("content-type", "text/html")
        self.send_header("content-length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args: object) -> None:
        pass


@pytest.fixture
def product() -> Iterator[int]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), Hello)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield server.server_address[1]
    server.shutdown()


async def test_the_testing_browser_opens_localhost_and_nothing_else_local(product: int) -> None:
    cfg = BrowserConfig(executable=os.environ.get("FORGE_BROWSER__EXECUTABLE"), timeout_s=10)
    factory = PlaywrightBrowsers(cfg, loopback=True)
    try:
        page = await factory.new_browser()
    except BrowserError as err:
        if os.environ.get("FORGE_REQUIRE_BROWSER"):
            raise
        pytest.skip(f"no browser: {err}")
    try:
        assert (await page.open(f"http://localhost:{product}/")).title == "Orders"
        assert (await page.open(f"http://127.0.0.1:{product}/")).title == "Orders"
        with pytest.raises(BrowserError):
            await page.open(f"http://192.168.255.254:{product}/")  # the LAN: blocked
    finally:
        await page.close()
        await factory.close()
