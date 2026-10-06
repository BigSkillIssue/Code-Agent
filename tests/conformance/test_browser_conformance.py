"""Every Browser implementation must drive pages the same way (skipped without a browser)."""

import base64
import os
import threading
from collections.abc import AsyncIterator, Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

import pytest

from forge.config import BrowserConfig
from forge.local.playwright_browser import PlaywrightBrowsers
from forge.ports import Browser, BrowserError

PAGES = {
    "/": "<title>Home</title><h1>Welcome</h1><a href='/docs'>Read the docs</a>",
    "/docs": "<title>Docs</title><label>Search <input name='q'></label>"
    "<script>document.querySelector('input').addEventListener('keydown', e => {"
    "if (e.key === 'Enter') location.href = '/search?q=' + e.target.value })</script>",
    "/long": "<title>Long</title>" + "<p>line</p>" * 400 + "<p id='end'>The end</p>",
}


class Pages(BaseHTTPRequestHandler):
    hits: list[str] = []

    def do_GET(self) -> None:
        parts = urlsplit(self.path)
        Pages.hits.append(parts.path)
        if parts.path == "/search":
            query = parse_qs(parts.query).get("q", [""])[0]
            body = f"<title>Results</title><p>You searched for {query}</p>"
        else:
            body = PAGES.get(parts.path, "<title>Missing</title>")
        data = body.encode()
        self.send_response(200)
        self.send_header("content-type", "text/html; charset=utf-8")
        self.send_header("content-length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args: object) -> None:
        pass


@pytest.fixture
def site() -> Iterator[str]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), Pages)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    Pages.hits = []
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()


def config() -> BrowserConfig:
    return BrowserConfig(executable=os.environ.get("FORGE_BROWSER__EXECUTABLE"), timeout_s=10)


async def started(allow_local: bool) -> tuple[PlaywrightBrowsers, Browser]:
    factory = PlaywrightBrowsers(config(), allow_local=allow_local)
    try:
        return factory, await factory.new_browser()
    except BrowserError as err:
        if os.environ.get(
            "FORGE_REQUIRE_BROWSER"
        ):  # CI installs Chromium; skipping would hide bugs
            raise
        pytest.skip(f"no browser: {err}")


@pytest.fixture(params=["playwright"])
async def browser(request: pytest.FixtureRequest) -> AsyncIterator[Browser]:
    factory, page = await started(allow_local=True)
    yield page
    await page.close()
    await factory.close()


async def test_open_shows_the_page(browser: Browser, site: str) -> None:
    view = await browser.open(site + "/")
    assert view.title == "Home" and view.url == site + "/"
    assert view.image is not None and view.image.media_type == "image/jpeg"
    assert base64.b64decode(view.image.data_b64)[:2] == b"\xff\xd8"  # a JPEG
    assert "Welcome" in await browser.read()


async def test_click_type_and_back(browser: Browser, site: str) -> None:
    await browser.open(site + "/")
    docs = await browser.click("Read the docs")
    assert docs.title == "Docs"
    results = await browser.type("Search", "asyncio", submit=True)
    assert "You searched for asyncio" in await browser.read()
    assert results.url.endswith("/search?q=asyncio")
    back = await browser.back()
    assert back.title == "Docs"


async def test_scroll_and_points(browser: Browser, site: str) -> None:
    await browser.open(site + "/long")
    view = await browser.scroll(5000)
    assert view.title == "Long"
    await browser.click("10,10")  # clicking empty space is harmless


async def test_a_missing_element_is_a_browser_error(browser: Browser, site: str) -> None:
    await browser.open(site + "/")
    with pytest.raises(BrowserError):
        await browser.click("css=#does-not-exist")


async def test_local_requests_are_blocked_by_default(site: str) -> None:
    factory, page = await started(allow_local=False)
    try:
        await page.open(f"data:text/html,<img src='{site}/tracker'><p>x</p>")
        with pytest.raises(BrowserError):
            await page.open(site + "/")
    finally:
        await page.close()
        await factory.close()
    assert Pages.hits == []
