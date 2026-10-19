"""The Browser port with Playwright: one Chromium per session, a fresh context per agent."""

import asyncio
import base64
import contextlib
import re
import subprocess
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

from forge.config import BrowserConfig
from forge.ports import Browser, BrowserError, PageView
from forge.providers.base import ImagePart
from forge.runtime.web import is_ip, is_local, resolve_host

if TYPE_CHECKING:
    from playwright.async_api import Browser as PwBrowser
    from playwright.async_api import (
        BrowserContext,
        Page,
        Playwright,
        Response,
        Route,
        WebSocketRoute,
    )

INSTALL_HINT = 'run `forge browser install` once, or set [browser] channel = "chrome"'
MAX_TEXT = 20_000
POINT = re.compile(r"^\s*(\d+)\s*,\s*(\d+)\s*$")
UNCHECKED_SCHEMES = ("data", "blob", "about")


class PlaywrightBrowsers:
    """BrowserFactory: starts Chromium on first use; every agent gets its own context."""

    def __init__(self, cfg: BrowserConfig, allow_local: bool = False) -> None:
        self.cfg = cfg
        self.allow_local = allow_local  # tests serve pages from localhost
        self._playwright: Playwright | None = None
        self._browser: PwBrowser | None = None

    async def new_browser(self, viewport: tuple[int, int] | None = None) -> Browser:
        """A fresh page in its own context: no cookies, no downloads, local hosts blocked
        (unless allowed); `viewport` (width, height) replaces the configured size."""
        browser = await self._launch()
        width, height = viewport or (self.cfg.viewport_width, self.cfg.viewport_height)
        context = await browser.new_context(
            viewport={"width": width, "height": height},
            accept_downloads=False,
            service_workers="block",
        )
        context.set_default_timeout(self.cfg.timeout_s * 1000)
        await context.route("**/*", self._guard)
        await context.route_web_socket("**/*", self._guard_socket)
        return PlaywrightPage(
            context, await context.new_page(), check_addresses=not self.allow_local
        )

    async def close(self) -> None:
        """Stop the browser and Playwright."""
        if self._browser is not None:
            await self._browser.close()
        if self._playwright is not None:
            await self._playwright.stop()
        self._browser = self._playwright = None

    async def _launch(self) -> "PwBrowser":
        if self._browser is not None:
            return self._browser
        try:
            from playwright.async_api import async_playwright
        except ImportError as exc:
            raise BrowserError(
                "Playwright is not installed", hint="pip install playwright"
            ) from exc
        self._playwright = await async_playwright().start()
        try:
            self._browser = await self._playwright.chromium.launch(
                headless=self.cfg.headless,
                channel=self.cfg.channel,
                executable_path=self.cfg.executable,
            )
        except Exception as exc:
            await self.close()
            raise BrowserError(
                f"the browser could not start: {first_line(exc)}", INSTALL_HINT
            ) from exc
        return self._browser

    async def _guard(self, route: "Route") -> None:
        """Abort requests to local or private hosts (the browser must not reach the LAN)."""
        parts = urlsplit(route.request.url)
        if parts.scheme in UNCHECKED_SCHEMES or self.allow_local:
            await route.continue_()
            return
        # checked for every request, not remembered: a name may point elsewhere a moment later
        if await host_is_local(parts.hostname or ""):
            await route.abort("blockedbyclient")
        else:
            await route.continue_()

    async def _guard_socket(self, socket: "WebSocketRoute") -> None:
        """WebSockets bypass `route`: the same check before they connect."""
        if self.allow_local or not await host_is_local(urlsplit(socket.url).hostname or ""):
            socket.connect_to_server()
        else:
            await socket.close(code=1008, reason="local or private address")


async def host_is_local(host: str) -> bool:
    """True for localhost, single-label names and names resolving to private addresses."""
    if not host or host == "localhost" or ("." not in host and not is_ip(host)):
        return True
    try:
        addresses = [host] if is_ip(host) else await resolve_host(host)
    except OSError:
        return True  # unknown: the browser might resolve it differently, so it counts as local
    return not addresses or any(is_local(address) for address in addresses)


class PlaywrightPage:
    """Browser: one page; every action waits for the page to settle, then shows it."""

    def __init__(
        self, context: "BrowserContext", page: "Page", *, check_addresses: bool = False
    ) -> None:
        self.context = context
        self.page = page
        # The request guard checks a name before Chromium resolves it again; the address a
        # response really came from is checked too, so a page from a local address is not shown.
        self._checks: list[asyncio.Task[None]] = []
        self._local_answer = ""
        if check_addresses:
            context.on("response", self._note_response)

    def _note_response(self, response: "Response") -> None:
        self._checks.append(asyncio.ensure_future(self._check_address(response)))

    async def _check_address(self, response: "Response") -> None:
        with contextlib.suppress(Exception):  # a response without an address is not local
            address = await response.server_addr()
            if address and is_local(address["ipAddress"]):
                self._local_answer = address["ipAddress"]

    async def _refuse_local(self) -> None:
        """Raise when anything on the page came from a local or private address."""
        checks, self._checks = self._checks, []
        await asyncio.gather(*checks)
        if self._local_answer:
            answer, self._local_answer = self._local_answer, ""
            with contextlib.suppress(Exception):
                await self.page.goto("about:blank")
            raise BrowserError(f"the page came from a local or private address ({answer})")

    async def open(self, url: str) -> PageView:
        """Load `url`."""
        await self._do(self.page.goto(url, wait_until="domcontentloaded"))
        return await self.view()

    async def click(self, target: str) -> PageView:
        """Click a visible text, a `css=` selector or an `x,y` point."""
        point = POINT.match(target)
        if point:
            await self._do(self.page.mouse.click(int(point[1]), int(point[2])))
        else:
            await self._do(self._locate(target).click())
        return await self._settled()

    async def type(self, target: str, text: str, submit: bool) -> PageView:
        """Fill a field (found by label, placeholder or `css=`) and optionally press Enter."""
        field = self._field(target)
        await self._do(field.fill(text))
        if submit:
            await self._do(field.press("Enter"))
        return await self._settled()

    async def scroll(self, pixels: int) -> PageView:
        """Scroll down (positive) or up (negative)."""
        await self._do(self.page.mouse.wheel(0, pixels))
        return await self._settled()

    async def back(self) -> PageView:
        """Go back one page."""
        await self._do(self.page.go_back(wait_until="domcontentloaded"))
        return await self.view()

    async def view(self) -> PageView:
        """URL, title and a screenshot of what is visible."""
        await self._refuse_local()
        shot = await self._do(self.page.screenshot(type="jpeg", quality=70))
        image = ImagePart(media_type="image/jpeg", data_b64=base64.b64encode(shot).decode("ascii"))
        return PageView(url=self.page.url, title=await self.page.title(), image=image)

    async def read(self) -> str:
        """The visible text of the page, capped."""
        await self._refuse_local()
        text: str = await self._do(self.page.inner_text("body"))
        return text if len(text) <= MAX_TEXT else text[:MAX_TEXT] + "\n[text truncated]"

    async def close(self) -> None:
        """Close this agent's context."""
        await self.context.close()

    def _locate(self, target: str) -> Any:
        if target.startswith("css="):
            return self.page.locator(target[4:]).first
        return self.page.get_by_text(target).first

    def _field(self, target: str) -> Any:
        if target.startswith("css="):
            return self.page.locator(target[4:]).first
        by_label = self.page.get_by_label(target)
        return by_label.or_(self.page.get_by_placeholder(target)).first

    async def _settled(self) -> PageView:
        with contextlib.suppress(Exception):  # a page that keeps loading is still worth a look
            await self.page.wait_for_load_state("domcontentloaded", timeout=5000)
        return await self.view()

    async def _do(self, action: Any) -> Any:
        try:
            return await action
        except Exception as exc:
            raise BrowserError(first_line(exc)) from exc


def first_line(exc: Exception) -> str:
    """Playwright errors carry a long call log; the first line says what happened."""
    return (str(exc).strip().splitlines() or [type(exc).__name__])[0][:300]


def install_chromium() -> int:
    """Download Playwright's Chromium (`forge browser install`); returns the exit code."""
    from playwright._impl._driver import compute_driver_executable, get_driver_env

    node, cli = compute_driver_executable()
    return subprocess.run([node, cli, "install", "chromium"], env=get_driver_env()).returncode
