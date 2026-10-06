"""The browser agent: browser tools over the Browser port (a FakeBrowser here)."""

from pathlib import Path
from typing import Any

import pytest

from forge.agent import run_agent
from forge.config import BrowserConfig, ForgeConfig
from forge.ctx import Ctx
from forge.ports import Browser, BrowserError, PageView
from forge.providers.base import Capabilities, ImagePart, ToolCall, ToolResult
from forge.providers.fake import FakeProvider, FakeToolCall, FakeTurn
from forge.providers.registry import register_provider
from forge.runtime import web
from forge.tools import agent_tools, call_tool
from support import make_ctx

SHOT = ImagePart(media_type="image/jpeg", data_b64="AAAA")


class FakeBrowser:
    """Records actions; every page is 'Example' at the last opened URL."""

    def __init__(self, fail: str = "") -> None:
        self.actions: list[tuple[Any, ...]] = []
        self.url = "about:blank"
        self.fail = fail
        self.closed = False

    async def _view(self, *action: Any) -> PageView:
        self.actions.append(action)
        if self.fail:
            raise BrowserError(self.fail)
        return PageView(url=self.url, title="Example", image=SHOT)

    async def open(self, url: str) -> PageView:
        self.url = url
        return await self._view("open", url)

    async def click(self, target: str) -> PageView:
        return await self._view("click", target)

    async def type(self, target: str, text: str, submit: bool) -> PageView:
        return await self._view("type", target, text, submit)

    async def scroll(self, pixels: int) -> PageView:
        return await self._view("scroll", pixels)

    async def back(self) -> PageView:
        return await self._view("back")

    async def view(self) -> PageView:
        return await self._view("view")

    async def read(self) -> str:
        self.actions.append(("read",))
        return "Example Domain\nThis domain is for examples."

    async def close(self) -> None:
        self.closed = True


class FakeBrowsers:
    """BrowserFactory handing out FakeBrowsers."""

    def __init__(self, fail: str = "") -> None:
        self.made: list[FakeBrowser] = []
        self.fail = fail

    async def new_browser(self) -> Browser:
        self.made.append(FakeBrowser(self.fail))
        return self.made[-1]

    async def close(self) -> None:
        pass


@pytest.fixture(autouse=True)
def public_dns(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_resolve(host: str) -> list[str]:
        return ["10.1.2.3"] if "internal" in host else ["93.184.216.34"]

    monkeypatch.setattr(web, "resolve_host", fake_resolve)


def browser_ctx(root: Path, **cfg: Any) -> tuple[Ctx, FakeBrowsers]:
    ctx = make_ctx(root, cfg=ForgeConfig(**cfg), role="browser")
    factory = FakeBrowsers()
    ctx.state.browser_factory = factory
    return ctx, factory


async def run(ctx: Ctx, name: str, **arguments: Any) -> ToolResult:
    return await call_tool(ctx, ToolCall(id="c1", name=name, arguments=arguments))


def test_only_the_browser_role_gets_browser_tools(ctx: Ctx) -> None:
    browser_tools = {t.name for t in agent_tools(ctx, "browser")}
    assert browser_tools == {
        "browser_open",
        "browser_click",
        "browser_type",
        "browser_scroll",
        "browser_back",
        "browser_screenshot",
        "browser_read",
        "web_search",
    }
    for role in ("coder", "researcher", "explore"):
        assert not any(t.group == "browser" for t in agent_tools(ctx, role))


async def test_open_shows_title_url_and_a_screenshot(tmp_project: Path) -> None:
    ctx, factory = browser_ctx(tmp_project)
    result = await run(ctx, "browser_open", url="https://example.com/docs")
    assert result.ok and result.images == [SHOT]
    assert result.text.splitlines()[:2] == ["title: Example", "url: https://example.com/docs"]
    assert "1280x800" in result.text
    assert factory.made[0].actions == [("open", "https://example.com/docs")]


async def test_local_addresses_are_refused(tmp_project: Path) -> None:
    ctx, factory = browser_ctx(tmp_project)
    for url in ("http://localhost:8000/", "https://db.internal/", "file:///etc/passwd"):
        result = await run(ctx, "browser_open", url=url)
        assert result.code == "invalid_args", url
    assert not factory.made or not factory.made[0].actions


async def test_actions_reuse_the_agents_browser(tmp_project: Path) -> None:
    ctx, factory = browser_ctx(tmp_project)
    await run(ctx, "browser_open", url="https://example.com/")
    await run(ctx, "browser_click", target="More information")
    await run(ctx, "browser_type", target="Search", text="asyncio", submit=True)
    await run(ctx, "browser_scroll", pixels=-300)
    await run(ctx, "browser_back")
    read = await run(ctx, "browser_read")
    assert len(factory.made) == 1
    assert factory.made[0].actions[1:] == [
        ("click", "More information"),
        ("type", "Search", "asyncio", True),
        ("scroll", -300),
        ("back",),
        ("read",),
    ]
    assert "Example Domain" in read.text and not read.images


async def test_screenshots_stop_at_the_limit(tmp_project: Path) -> None:
    ctx, _ = browser_ctx(tmp_project, browser=BrowserConfig(max_screenshots=2))
    results = [await run(ctx, "browser_screenshot") for _ in range(3)]
    assert [len(r.images) for r in results] == [1, 1, 0]
    assert "screenshot limit" in results[2].text


async def test_browser_errors_become_tool_errors(tmp_project: Path) -> None:
    ctx = make_ctx(tmp_project, role="browser")
    ctx.state.browser_factory = FakeBrowsers(fail="Timeout 30000ms exceeded. waiting for X")
    result = await run(ctx, "browser_click", target="X")
    assert result.code == "not_found"


async def test_without_a_browser_the_tools_are_unsupported(tmp_project: Path) -> None:
    result = await run(make_ctx(tmp_project, role="browser"), "browser_screenshot")
    assert result.code == "unsupported"


def research_ctx(root: Path, vision: bool = True) -> tuple[Ctx, FakeProvider, FakeBrowsers]:
    open_page = FakeTurn(
        tool_calls=[FakeToolCall(name="browser_open", arguments={"url": "https://example.com/"})]
    )
    fake = FakeProvider(
        roles={
            "coder": [
                FakeTurn(
                    tool_calls=[
                        FakeToolCall(
                            name="research",
                            arguments={"question": "What is on it?", "browser": True},
                        )
                    ]
                ),
                FakeTurn(text="Done."),
            ],
            "browser": [open_page, FakeTurn(text="It says Example. https://example.com/")],
        },
        caps=Capabilities(context_window=128_000, max_output=8_192, vision=vision),
    )
    cfg = ForgeConfig(roles={"coder": ["fake/coder"], "browser": ["fake/browser"]})
    register_provider(cfg, fake)
    ctx = make_ctx(root, cfg=cfg)
    factory = FakeBrowsers()
    ctx.state.browser_factory = factory
    return ctx, fake, factory


async def test_research_with_browser_runs_the_browser_agent(tmp_project: Path) -> None:
    ctx, fake, factory = research_ctx(tmp_project)
    result = await run_agent(ctx, "look at example.com")
    report = result.messages[2].tool_result
    assert report is not None and report.ok
    assert "agent a1 (browser) finished: done" in report.text
    assert "It says Example." in report.text
    request = next(r for r in fake.requests if r.model == "browser")
    assert "You are a browser sub-agent" in request.system
    assert factory.made[0].closed and not ctx.state.browsers


async def test_browser_research_needs_a_vision_model(tmp_project: Path) -> None:
    ctx, _, _ = research_ctx(tmp_project, vision=False)
    result = await run(ctx, "research", question="q", browser=True)
    assert result.code == "unsupported" and "cannot see images" in result.text
