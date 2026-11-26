"""SEC-K6: what the browser can reach. Every connection goes through Forge's proxy, so a
redirect cannot carry it to the LAN; this machine's servers are reachable only while the agent
is on one of them; read-only roles never get the testing browser."""

import asyncio
import os
import threading
from collections.abc import AsyncIterator, Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest

from forge.config import BrowserConfig, ForgeConfig
from forge.local.playwright_browser import Gate, PlaywrightBrowsers, PlaywrightPage
from forge.ports import BrowserError
from forge.providers.base import ToolCall
from forge.runtime import web
from forge.tools import call_tool, resolve_grant
from support import make_ctx
from test_browser import FakeBrowsers

LAN = "10.255.255.1:8080"  # private and not routed: a connection attempt would hang
HITS: list[str] = []


class Product(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        self._answer()

    def do_POST(self) -> None:
        self._answer()

    def _answer(self) -> None:
        HITS.append(f"{self.command} {self.path}")
        parts = urlsplit(self.path)
        if parts.path == "/redirect":
            self.send_response(302)
            self.send_header("location", parse_qs(parts.query)["to"][0])
            self.end_headers()
            return
        script = f"fetch('/redirect?to=http://{LAN}/secret').then(() => document.title = 'read',"
        script += " () => document.title = 'refused')"
        data = f"<title>Orders</title><script>{script}</script>".encode()
        self.send_response(200)
        self.send_header("content-type", "text/html")
        self.send_header("content-length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args: object) -> None:
        pass


@pytest.fixture
def product() -> Iterator[int]:
    HITS.clear()
    server = ThreadingHTTPServer(("127.0.0.1", 0), Product)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield server.server_address[1]
    server.shutdown()


@pytest.fixture
async def testing_page() -> AsyncIterator[PlaywrightPage]:
    cfg = BrowserConfig(executable=os.environ.get("FORGE_BROWSER__EXECUTABLE"), timeout_s=10)
    factory = PlaywrightBrowsers(cfg, loopback=True)
    try:
        page = await factory.new_browser()
    except BrowserError as err:
        if os.environ.get("FORGE_REQUIRE_BROWSER"):
            raise
        pytest.skip(f"no browser: {err}")
    assert isinstance(page, PlaywrightPage)
    yield page
    await page.close()
    await factory.close()


async def title_settles(page: PlaywrightPage, *titles: str) -> str:
    for _ in range(50):
        title = await page.page.title()
        if title in titles:
            return title
        await asyncio.sleep(0.1)
    return await page.page.title()


async def test_a_redirect_does_not_carry_the_browser_to_the_lan(
    testing_page: PlaywrightPage, product: int
) -> None:
    await testing_page.open(f"http://127.0.0.1:{product}/")
    assert await title_settles(testing_page, "read", "refused") == "refused"
    with pytest.raises(BrowserError, match=r"10\.255\.255\.1 is a local or private address"):
        await testing_page.open(f"http://localhost:{product}/redirect?to=http://{LAN}/top")


async def test_a_page_from_elsewhere_cannot_reach_this_machine(
    testing_page: PlaywrightPage, product: int
) -> None:
    attack = (
        f"<img src='http://127.0.0.1:{product}/img'>"
        f"<form method=post action='http://localhost:{product}/csrf'></form>"
        "<script>document.forms[0].submit()</script>"
    )
    await testing_page.open("data:text/html," + attack)
    await asyncio.sleep(1)
    assert HITS == []  # neither the picture nor the form reached the product
    view = await testing_page.open(f"http://127.0.0.1:{product}/")  # opening it reopens it
    assert view.title in ("Orders", "refused")


def test_leaving_this_machine_closes_the_way_back() -> None:
    gate = Gate(loopback=True)
    page = PlaywrightPage.__new__(PlaywrightPage)
    page.gate = gate
    page._navigated(SimpleNamespace(url="http://localhost:3000/", parent_frame=None))  # type: ignore[arg-type]
    page._navigated(SimpleNamespace(url="https://x.test/", parent_frame=object()))  # type: ignore[arg-type]
    assert gate.loopback  # its own pages, and frames inside them, keep it open
    page._navigated(SimpleNamespace(url="https://checkout.example/pay", parent_frame=None))  # type: ignore[arg-type]
    assert not gate.loopback


async def test_read_only_roles_get_the_public_browser_even_when_granted(
    tmp_project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def public(host: str) -> list[str]:
        return ["93.184.216.34"]

    monkeypatch.setattr(web, "resolve_host", public)
    lead = make_ctx(tmp_project, cfg=ForgeConfig())
    research, testing = FakeBrowsers(), FakeBrowsers()
    lead.state.browser_factory, lead.state.test_browsers = research, testing
    explore = make_ctx(tmp_project, cfg=lead.cfg, agent_id="a1", role="explore")
    explore.state = lead.state
    explore.granted_tools = resolve_grant(lead, ["browser"])
    call = ToolCall(id="c1", name="browser_open", arguments={"url": "http://localhost:5173/"})
    assert (await call_tool(explore, call)).code == "invalid_args"
    call.arguments = {"url": "https://example.com/"}
    result = await call_tool(explore, call)
    assert result.ok, result.text
    assert testing.made == [] and len(research.made) == 1
