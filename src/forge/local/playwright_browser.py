"""The Browser port with Playwright: one Chromium per session, a fresh context per agent."""

import base64
import contextlib
import re
import subprocess
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

from forge.config import BrowserConfig
from forge.local.browser_proxy import GuardedProxy, browser_policy
from forge.ports import Browser, BrowserError, PageView
from forge.providers.base import ImagePart
from forge.runtime.web import is_ip, is_local, is_loopback_host, resolve_host

if TYPE_CHECKING:
    from playwright.async_api import Browser as PwBrowser
    from playwright.async_api import (
        BrowserContext,
        Frame,
        Page,
        Playwright,
        Route,
        WebSocketRoute,
    )

INSTALL_HINT = 'run `forge browser install` once, or set [browser] channel = "chrome"'
MAX_TEXT = 20_000
POINT = re.compile(r"^\s*(\d+)\s*,\s*(\d+)\s*$")
UNCHECKED_SCHEMES = ("data", "blob", "about")
# WebRTC sends UDP past any proxy; only proxied (TCP) connections are allowed
LAUNCH_ARGS = ["--force-webrtc-ip-handling-policy=disable_non_proxied_udp"]


@dataclass
class Gate:
    """Whether an agent's browser may reach this machine's own servers right now (S78): only
    while the agent has opened one of them, never while it shows a page from elsewhere."""

    loopback: bool = False


class PlaywrightBrowsers:
    """BrowserFactory: starts Chromium on first use; every agent gets its own context."""

    def __init__(
        self, cfg: BrowserConfig, allow_local: bool = False, loopback: bool = False
    ) -> None:
        self.cfg = cfg
        self.allow_local = allow_local  # tests serve pages from localhost
        # S78: agents test the product they run on this machine; the LAN stays blocked
        self.loopback = loopback
        self._playwright: Playwright | None = None
        self._browser: PwBrowser | None = None

    async def new_browser(self, viewport: tuple[int, int] | None = None) -> Browser:
        """A fresh page in its own context: no cookies, no downloads, local hosts blocked
        (unless allowed); `viewport` (width, height) replaces the configured size."""
        browser = await self._launch()
        width, height = viewport or (self.cfg.viewport_width, self.cfg.viewport_height)
        gate = Gate()
        options: dict[str, Any] = {}
        proxy = None
        if not self.allow_local:
            # every connection, redirect hops and WebSockets included, is checked by address
            proxy = GuardedProxy(browser_policy(lambda: self.loopback and gate.loopback))
            options["proxy"] = {"server": await proxy.start()}
        context = await browser.new_context(
            viewport={"width": width, "height": height},
            accept_downloads=False,
            service_workers="block",
            **options,
        )
        context.set_default_timeout(self.cfg.timeout_s * 1000)

        async def guard(route: "Route") -> None:
            await self._guard(route, gate)

        async def guard_socket(socket: "WebSocketRoute") -> None:
            await self._guard_socket(socket, gate)

        await context.route("**/*", guard)
        await context.route_web_socket("**/*", guard_socket)
        page = await context.new_page()
        return PlaywrightPage(context, page, proxy=proxy, gate=gate if self.loopback else None)

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
                args=[] if self.allow_local else LAUNCH_ARGS,
            )
        except Exception as exc:
            await self.close()
            raise BrowserError(
                f"the browser could not start: {first_line(exc)}", INSTALL_HINT
            ) from exc
        return self._browser

    async def _guard(self, route: "Route", gate: Gate) -> None:
        """Abort requests to local or private hosts (the browser must not reach the LAN)."""
        parts = urlsplit(route.request.url)
        if parts.scheme in UNCHECKED_SCHEMES or self._allowed(parts.hostname or "", gate):
            await route.continue_()
            return
        # checked for every request, not remembered: a name may point elsewhere a moment later
        if await host_is_local(parts.hostname or ""):
            await route.abort("blockedbyclient")
        else:
            await route.continue_()

    async def _guard_socket(self, socket: "WebSocketRoute", gate: Gate) -> None:
        """WebSockets bypass `route`: the same check before they connect."""
        host = urlsplit(socket.url).hostname or ""
        if self._allowed(host, gate) or not await host_is_local(host):
            socket.connect_to_server()
        else:
            await socket.close(code=1008, reason="local or private address")

    def _allowed(self, host: str, gate: Gate) -> bool:
        """Hosts let through unchecked: everything (tests), or this machine's own servers
        while the agent is on one of them."""
        return self.allow_local or (self.loopback and gate.loopback and is_loopback_host(host))


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
        self,
        context: "BrowserContext",
        page: "Page",
        *,
        proxy: GuardedProxy | None = None,
        gate: Gate | None = None,
    ) -> None:
        self.context = context
        self.page = page
        self.proxy = proxy  # None only where local hosts are allowed (tests, previews)
        self.gate = gate  # None: this browser never opens this machine's servers
        if gate is not None:
            context.on("page", self._watch)
            self._watch(page)

    def _watch(self, page: "Page") -> None:
        page.on("framenavigated", self._navigated)

    def _navigated(self, frame: "Frame") -> None:
        """A page that leaves this machine's servers closes the way back to them: what a
        product links or redirects to may be anyone's page."""
        parts = urlsplit(frame.url)
        if frame.parent_frame is not None or parts.scheme not in ("http", "https"):
            return
        if self.gate is not None and not is_loopback_host(parts.hostname or ""):
            self.gate.loopback = False

    async def _aim(self, url: str) -> None:
        """Before the agent opens `url`: this machine's servers become reachable only for one
        of them, and only after the page shown before has gone (its scripts end with it)."""
        if self.gate is None:
            return
        wanted = is_loopback_host(urlsplit(url).hostname or "")
        if wanted and not self.gate.loopback:
            await self._do(self.page.goto("about:blank"))
            for other in self.context.pages:
                if other is not self.page:
                    await other.close()
        self.gate.loopback = wanted

    async def open(self, url: str) -> PageView:
        """Load `url`."""
        await self._aim(url)
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
        if self.proxy is not None:
            await self.proxy.close()

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
        if self.proxy is not None:
            self.proxy.take_refused()  # only what this action asked for explains its failure
        try:
            return await action
        except Exception as exc:
            refused = self.proxy.take_refused() if self.proxy is not None else []
            if refused:
                raise BrowserError(
                    f"{refused[-1]} is a local or private address: the browser does not go there"
                ) from exc
            raise BrowserError(first_line(exc)) from exc


def first_line(exc: Exception) -> str:
    """Playwright errors carry a long call log; the first line says what happened."""
    return (str(exc).strip().splitlines() or [type(exc).__name__])[0][:300]


def install_chromium() -> int:
    """Download Playwright's Chromium (`forge browser install`); returns the exit code."""
    from playwright._impl._driver import compute_driver_executable, get_driver_env

    node, cli = compute_driver_executable()
    return subprocess.run([node, cli, "install", "chromium"], env=get_driver_env()).returncode
