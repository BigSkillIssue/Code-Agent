"""The Browser port with Playwright: one Chromium per session, a fresh context per agent."""

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
    from playwright.async_api import BrowserContext, Page, Playwright, Route

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
        self._local_hosts: dict[str, bool] = {}

    async def new_browser(self) -> Browser:
        """A fresh page in its own context: no cookies, no downloads, local hosts blocked."""
        browser = await self._launch()
        context = await browser.new_context(
            viewport={"width": self.cfg.viewport_width, "height": self.cfg.viewport_height},
            accept_downloads=False,
            service_workers="block",
        )
        context.set_default_timeout(self.cfg.timeout_s * 1000)
        await context.route("**/*", self._guard)
        return PlaywrightPage(context, await context.new_page())

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
        host = parts.hostname or ""
        if host not in self._local_hosts:
            self._local_hosts[host] = await host_is_local(host)
        if self._local_hosts[host]:
            await route.abort("blockedbyclient")
        else:
            await route.continue_()


async def host_is_local(host: str) -> bool:
    """True for localhost, single-label names and names resolving to private addresses."""
    if not host or host == "localhost" or ("." not in host and not is_ip(host)):
        return True
    try:
        addresses = [host] if is_ip(host) else await resolve_host(host)
    except OSError:
        return False  # the browser will fail to connect on its own
    return any(is_local(address) for address in addresses)


class PlaywrightPage:
    """Browser: one page; every action waits for the page to settle, then shows it."""

    def __init__(self, context: "BrowserContext", page: "Page") -> None:
        self.context = context
        self.page = page

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
        shot = await self._do(self.page.screenshot(type="jpeg", quality=70))
        image = ImagePart(media_type="image/jpeg", data_b64=base64.b64encode(shot).decode("ascii"))
        return PageView(url=self.page.url, title=await self.page.title(), image=image)

    async def read(self) -> str:
        """The visible text of the page, capped."""
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
