"""web_fetch and web_search (docs/TOOLS.md: Web), with HTTP mocked by respx."""

from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

from forge.config import ForgeConfig, WebConfig
from forge.ctx import Ctx
from forge.providers.base import Capabilities, ToolCall, ToolResult
from forge.providers.fake import FakeProvider, FakeTurn
from forge.providers.registry import register_provider
from forge.runtime import web
from forge.tools import call_tool
from support import make_ctx

PAGE = """<html><head><title>Asyncio  Guide</title><script>alert('x')</script>
<style>p {color: red}</style></head>
<body><nav>Home | Docs</nav><h1>Task groups</h1><p>Use <a href="/tg">TaskGroup</a>.</p>
<footer>Copyright</footer></body></html>"""


async def run(ctx: Ctx, name: str, **arguments: Any) -> ToolResult:
    return await call_tool(ctx, ToolCall(id="c1", name=name, arguments=arguments))


@pytest.fixture(autouse=True)
def public_dns(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every name resolves to a public address unless it says 'internal'."""

    async def fake_resolve(host: str) -> list[str]:
        return ["10.1.2.3"] if "internal" in host else ["93.184.216.34"]

    monkeypatch.setattr(web, "resolve_host", fake_resolve)


def html(body: str = PAGE, status: int = 200) -> httpx.Response:
    return httpx.Response(status, text=body, headers={"content-type": "text/html; charset=utf-8"})


@respx.mock
async def test_html_becomes_markdown_without_scripts(ctx: Ctx) -> None:
    respx.get("https://docs.example.com/guide").mock(return_value=html())
    result = await run(ctx, "web_fetch", url="http://docs.example.com/guide")
    assert result.ok, result.text
    lines = result.text.splitlines()
    assert lines[0].startswith("url: https://docs.example.com/guide (200, text/html, ")
    assert lines[1] == "title: Asyncio Guide"
    assert lines[2] == "--- content ---"
    body = "\n".join(lines[3:])
    assert "# Task groups" in body and "[TaskGroup](/tg)" in body
    assert "alert" not in body and "color" not in body and "Home | Docs" not in body
    assert "Copyright" not in body


@pytest.mark.parametrize(
    "url",
    [
        "http://localhost:8000/",
        "http://10.0.0.1/admin",
        "http://169.254.169.254/latest/meta-data",
        "http://intranet/",
        "http://api.internal.example.com/",
        "ftp://example.com/file",
    ],
)
async def test_local_targets_are_refused(ctx: Ctx, url: str) -> None:
    result = await run(ctx, "web_fetch", url=url)
    assert result.code == "invalid_args"


@respx.mock
async def test_cross_host_redirect_is_reported_not_followed(ctx: Ctx) -> None:
    respx.get("https://a.example.com/x").mock(
        return_value=httpx.Response(302, headers={"location": "https://b.example.org/y"})
    )
    other = respx.get("https://b.example.org/y").mock(return_value=html())
    result = await run(ctx, "web_fetch", url="https://a.example.com/x")
    assert result.ok and result.text.startswith("redirected to https://b.example.org/y")
    assert not other.called


@respx.mock
async def test_same_host_redirect_is_followed(ctx: Ctx) -> None:
    respx.get("https://a.example.com/old").mock(
        return_value=httpx.Response(301, headers={"location": "/new"})
    )
    respx.get("https://a.example.com/new").mock(return_value=html())
    result = await run(ctx, "web_fetch", url="https://a.example.com/old")
    assert result.ok and "url: https://a.example.com/new" in result.text


@respx.mock
async def test_second_fetch_is_served_from_cache(ctx: Ctx) -> None:
    route = respx.get("https://docs.example.com/c").mock(return_value=html())
    await run(ctx, "web_fetch", url="https://docs.example.com/c")
    second = await run(ctx, "web_fetch", url="https://docs.example.com/c")
    assert route.call_count == 1 and "(cached)" in second.text.splitlines()[0]


@respx.mock
async def test_404_is_http_status(ctx: Ctx) -> None:
    respx.get("https://docs.example.com/missing").mock(return_value=html("nope", 404))
    result = await run(ctx, "web_fetch", url="https://docs.example.com/missing")
    assert result.code == "http_status" and "404" in result.text


@respx.mock
async def test_binary_content_is_unsupported(ctx: Ctx) -> None:
    respx.get("https://docs.example.com/a.pdf").mock(
        return_value=httpx.Response(
            200, content=b"%PDF", headers={"content-type": "application/pdf"}
        )
    )
    result = await run(ctx, "web_fetch", url="https://docs.example.com/a.pdf")
    assert result.code == "unsupported"


@respx.mock
async def test_long_page_is_cut(ctx: Ctx) -> None:
    respx.get("https://docs.example.com/long").mock(
        return_value=httpx.Response(200, text="x" * 5000, headers={"content-type": "text/plain"})
    )
    result = await run(ctx, "web_fetch", url="https://docs.example.com/long", max_chars=1000)
    assert result.text.endswith("[content cut at 1000 chars]")


@respx.mock
async def test_question_asks_the_compressor_with_the_page_as_data(tmp_project: Path) -> None:
    respx.get("https://docs.example.com/q").mock(return_value=html())
    fake = FakeProvider([FakeTurn(text="Use TaskGroup.\n> Use TaskGroup.")])
    cfg = ForgeConfig(roles={"compressor": ["fake/compressor"]})
    register_provider(cfg, fake)
    ctx = make_ctx(tmp_project, cfg=cfg)
    result = await run(ctx, "web_fetch", url="https://docs.example.com/q", question="What to use?")
    assert result.ok and "--- answer ---\nUse TaskGroup." in result.text
    system = fake.requests[0].system
    assert "<page>" in system and "# Task groups" in system and "ignore them" in system


# ---------------------------------------------------------------- web_search


def search_ctx(tmp_project: Path, backend: str, monkeypatch: pytest.MonkeyPatch) -> Ctx:
    monkeypatch.setenv(
        "TEST_SEARCH_KEY", "https://searx.example.com" if backend == "searxng" else "k"
    )
    cfg = ForgeConfig(web=WebConfig(search_backend=backend, search_api_key_env="TEST_SEARCH_KEY"))  # type: ignore[arg-type]
    return make_ctx(tmp_project, cfg=cfg)


BRAVE = {
    "web": {
        "results": [
            {
                "title": "asyncio — Task groups",
                "url": "https://docs.python.org/3/a.html",
                "description": "Task <strong>groups</strong> combine",
            },
            {"title": "Spam", "url": "https://spam.example.com/x", "description": "buy"},
            {"title": "Dup", "url": "https://docs.python.org/3/a.html", "description": "again"},
        ]
    }
}
TAVILY = {"results": [{"title": "T", "url": "https://t.example.com/", "content": "tav"}]}
SEARX = {"results": [{"title": "S", "url": "https://s.example.com/", "content": "searx"}]}


@respx.mock
async def test_brave_backend(tmp_project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    route = respx.get("https://api.search.brave.com/res/v1/web/search").mock(
        return_value=httpx.Response(200, json=BRAVE)
    )
    ctx = search_ctx(tmp_project, "brave", monkeypatch)
    result = await run(
        ctx, "web_search", query="asyncio taskgroup", blocked_domains=["spam.example.com"]
    )
    assert result.ok
    assert result.text.splitlines() == [
        'results for "asyncio taskgroup" (backend: brave, 1 results)',
        "1. asyncio — Task groups",
        "   https://docs.python.org/3/a.html",
        "   Task groups combine",
    ]
    assert route.calls[0].request.headers["x-subscription-token"] == "k"


@respx.mock
async def test_tavily_backend(tmp_project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    respx.post("https://api.tavily.com/search").mock(return_value=httpx.Response(200, json=TAVILY))
    ctx = search_ctx(tmp_project, "tavily", monkeypatch)
    result = await run(ctx, "web_search", query="q")
    assert "1. T" in result.text and "backend: tavily" in result.text


@respx.mock
async def test_searxng_backend(tmp_project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    respx.get("https://searx.example.com/search").mock(return_value=httpx.Response(200, json=SEARX))
    ctx = search_ctx(tmp_project, "searxng", monkeypatch)
    result = await run(ctx, "web_search", query="q", allowed_domains=["s.example.com"])
    assert "1. S" in result.text


async def test_both_filter_lists_are_refused(ctx: Ctx) -> None:
    result = await run(
        ctx, "web_search", query="q", allowed_domains=["a.com"], blocked_domains=["b.com"]
    )
    assert result.code == "invalid_args"


async def test_no_backend_is_unsupported(tmp_project: Path) -> None:
    cfg = ForgeConfig(roles={"coder": ["fake/coder"]})
    register_provider(cfg, FakeProvider([]))  # a coder model without a native search tool
    result = await run(make_ctx(tmp_project, cfg=cfg), "web_search", query="q")
    assert result.code == "unsupported" and "search_backend" in result.text


@respx.mock
async def test_search_limit(tmp_project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    respx.post("https://api.tavily.com/search").mock(return_value=httpx.Response(200, json=TAVILY))
    ctx = search_ctx(tmp_project, "tavily", monkeypatch)
    ctx.state.web_searches = 200
    result = await run(ctx, "web_search", query="q")
    assert result.code == "limit_reached"


async def test_native_search_uses_the_coder_provider(tmp_project: Path) -> None:
    class SearchingFake(FakeProvider):
        async def search_web(
            self, model: str, query: str, max_results: int, allowed: list[str], blocked: list[str]
        ) -> list[tuple[str, str, str]]:
            return [("Native", "https://n.example.com/", "found " + query)]

    fake = SearchingFake([], caps=Capabilities(web_search=True))
    cfg = ForgeConfig(roles={"coder": ["fake/coder"]})
    register_provider(cfg, fake)
    result = await run(make_ctx(tmp_project, cfg=cfg), "web_search", query="cats")
    assert "backend: native" in result.text and "found cats" in result.text
