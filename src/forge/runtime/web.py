"""HTTP helpers for `web_fetch` and `web_search`: URL safety, fetching, caching, search backends.

These run inside the Forge process (not in the command sandbox), so the local-target checks
here are what keeps a model from reaching localhost or cloud metadata services.
"""

import asyncio
import hashlib
import ipaddress
import json
import re
import socket
import time
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlsplit, urlunsplit

import httpx

from forge import __version__
from forge.runtime.errors import ToolError

MAX_BYTES = 10 * 1024 * 1024
MAX_REDIRECTS = 5
CACHE_SECONDS = 15 * 60
TIMEOUT = httpx.Timeout(300.0, connect=10.0)
HEADERS = {
    "User-Agent": f"Forge/{__version__}",
    "Accept": "text/markdown, text/html;q=0.9, */*;q=0.5",
}
REMOVED_TAGS = ("script", "style", "nav", "footer", "header", "noscript", "iframe")
TEXT_TYPES = (
    "text/markdown",
    "text/plain",
    "application/json",
    "text/xml",
    "application/xml",
    "text/x-markdown",
)
LOCAL_HINT = "use bash with curl for local servers"

Resolver = Callable[[str], Awaitable[list[str]]]


@dataclass
class Page:
    """A fetched page, converted to text."""

    url: str
    status: int
    content_type: str
    size: int
    title: str
    content: str
    redirect: str = ""  # set when the server redirected to another host
    cached: bool = False


async def resolve_host(host: str) -> list[str]:
    """IP addresses of `host` (DNS)."""
    infos = await asyncio.get_running_loop().getaddrinfo(host, None, type=socket.SOCK_STREAM)
    return [str(info[4][0]) for info in infos]


async def checked_url(url: str, resolve: Resolver | None = None) -> str:
    """The URL upgraded to https; refuses non-http schemes and local or private hosts."""
    parts = urlsplit(url.strip())
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ToolError("invalid_args", "only full http(s) URLs can be fetched")
    host = parts.hostname
    if host == "localhost" or ("." not in host and not is_ip(host)):
        raise ToolError("invalid_args", f"{host} is a local host", hint=LOCAL_HINT)
    try:
        addresses = [host] if is_ip(host) else await (resolve or resolve_host)(host)
    except OSError as exc:
        raise ToolError("network", f"cannot resolve {host}: {exc}") from exc
    if any(is_local(a) for a in addresses):
        raise ToolError(
            "invalid_args", f"{host} points to a local or private address", hint=LOCAL_HINT
        )
    return urlunsplit(("https", parts.netloc, parts.path or "/", parts.query, ""))


def is_ip(host: str) -> bool:
    """True for an IPv4 or IPv6 literal."""
    try:
        ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        return False
    return True


def is_local(address: str) -> bool:
    """Private, loopback, link-local (incl. cloud metadata), reserved or unspecified."""
    ip = ipaddress.ip_address(address.split("%")[0].strip("[]"))
    return (
        ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_unspecified
    )


async def fetch_page(
    root: Path,
    url: str,
    *,
    resolve: Resolver | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> Page:
    """Fetch `url` (or its cached copy) and convert it; raises ToolError on failures."""
    url = await checked_url(url, resolve)
    cached = read_cache(root, url)
    if cached is not None:
        return cached
    try:
        async with httpx.AsyncClient(
            timeout=TIMEOUT, transport=transport, headers=HEADERS
        ) as client:
            page = await _get(client, url, resolve)
    except httpx.TimeoutException as exc:
        raise ToolError("timeout", f"{url} did not answer in time") from exc
    except httpx.HTTPError as exc:
        raise ToolError("network", f"{type(exc).__name__}: {exc}") from exc
    if not page.redirect:
        write_cache(root, page)
    return page


async def _get(client: httpx.AsyncClient, url: str, resolve: Resolver | None) -> Page:
    host = urlsplit(url).hostname
    for _ in range(MAX_REDIRECTS + 1):
        async with client.stream("GET", url) as response:
            if response.is_redirect:
                target = urljoin(url, response.headers.get("location", ""))
                if urlsplit(target).hostname != host:
                    return Page(
                        url=url,
                        status=response.status_code,
                        content_type="",
                        size=0,
                        title="",
                        content="",
                        redirect=target,
                    )
                url = await checked_url(target, resolve)
                continue
            if response.status_code >= 400:
                raise ToolError(
                    "http_status", f"{response.status_code} {response.reason_phrase} for {url}"
                )
            body = await read_limited(response)
            content_type = response.headers.get("content-type", "").split(";")[0].strip().lower()
            charset = response.charset_encoding or "utf-8"
            title, content = convert(body.decode(charset, errors="replace"), content_type)
            return Page(
                url=url,
                status=response.status_code,
                content_type=content_type,
                size=len(body),
                title=title,
                content=content,
            )
    raise ToolError("http_status", f"more than {MAX_REDIRECTS} redirects for {url}")


async def read_limited(response: httpx.Response) -> bytes:
    """The body, refusing pages over 10 MB."""
    chunks: list[bytes] = []
    size = 0
    async for chunk in response.aiter_bytes():
        size += len(chunk)
        if size > MAX_BYTES:
            raise ToolError("too_large", "the page is larger than 10 MB")
        chunks.append(chunk)
    return b"".join(chunks)


def convert(text: str, content_type: str) -> tuple[str, str]:
    """(title, content as Markdown or unchanged text); binary types are unsupported."""
    if content_type in ("text/html", "application/xhtml+xml"):
        return html_title(text), html_to_markdown(text)
    if content_type in TEXT_TYPES or content_type.endswith(("+json", "+xml")):
        return "", text
    raise ToolError(
        "unsupported",
        f"cannot show {content_type or 'unknown'} content",
        hint="only web pages and text are supported",
    )


def html_title(html: str) -> str:
    """The page's <title>, whitespace collapsed."""
    found = re.search(r"<title[^>]*>(.*?)</title>", html, re.IGNORECASE | re.DOTALL)
    return " ".join(found.group(1).split()) if found else ""


def html_to_markdown(html: str) -> str:
    """Markdown of the page without scripts, styles and navigation."""
    from markdownify import markdownify

    tags = "|".join(REMOVED_TAGS)
    html = re.sub(rf"<({tags})\b[^>]*>.*?</\1\s*>", "", html, flags=re.IGNORECASE | re.DOTALL)
    html = re.sub(r"<title[^>]*>.*?</title>", "", html, flags=re.IGNORECASE | re.DOTALL)
    markdown = markdownify(html, heading_style="ATX")
    return re.sub(r"\n{3,}", "\n\n", markdown).strip()


def cache_path(root: Path, url: str) -> Path:
    """Where a fetched page is cached."""
    return root / ".forge" / "cache" / "web" / f"{hashlib.sha256(url.encode()).hexdigest()}.json"


def read_cache(root: Path, url: str) -> Page | None:
    """A cached page younger than 15 minutes."""
    path = cache_path(root, url)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if time.time() - float(data.pop("fetched_at", 0)) > CACHE_SECONDS:
        return None
    return Page(**{**data, "cached": True})


def write_cache(root: Path, page: Page) -> None:
    """Cache a page; failures only cost a refetch."""
    path = cache_path(root, page.url)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({**asdict(page), "fetched_at": time.time()}), encoding="utf-8")
    except OSError:
        pass


def size_text(size: int) -> str:
    """412 B, 4.1 KB, 2.3 MB."""
    if size < 1024:
        return f"{size} B"
    if size < 1024 * 1024:
        return f"{size / 1024:.1f} KB"
    return f"{size / 1024 / 1024:.1f} MB"


# ----------------------------------------------------------------------------- search


@dataclass
class SearchResult:
    """One search hit."""

    title: str
    url: str
    snippet: str = ""


async def search_http(
    backend: str,
    key: str,
    query: str,
    max_results: int,
    allowed: list[str],
    blocked: list[str],
    transport: httpx.AsyncBaseTransport | None = None,
) -> list[SearchResult]:
    """Query brave, tavily or searxng (for searxng, `key` is the instance URL)."""
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(30.0), transport=transport) as client:
            if backend == "brave":
                return await _brave(client, key, query, max_results)
            if backend == "tavily":
                return await _tavily(client, key, query, max_results, allowed, blocked)
            return await _searxng(client, key, query)
    except httpx.HTTPStatusError as exc:
        raise ToolError("http_status", f"{backend} answered {exc.response.status_code}") from exc
    except httpx.HTTPError as exc:
        raise ToolError("network", f"{backend}: {type(exc).__name__}: {exc}") from exc


async def _brave(client: httpx.AsyncClient, key: str, query: str, count: int) -> list[SearchResult]:
    response = await client.get(
        "https://api.search.brave.com/res/v1/web/search",
        params={"q": query, "count": count},
        headers={"X-Subscription-Token": key, "Accept": "application/json"},
    )
    response.raise_for_status()
    hits = (response.json().get("web") or {}).get("results") or []
    return [
        SearchResult(h.get("title", ""), h.get("url", ""), strip_tags(h.get("description", "")))
        for h in hits
    ]


async def _tavily(
    client: httpx.AsyncClient,
    key: str,
    query: str,
    count: int,
    allowed: list[str],
    blocked: list[str],
) -> list[SearchResult]:
    body: dict[str, Any] = {"query": query, "max_results": count}
    if allowed:
        body["include_domains"] = allowed
    if blocked:
        body["exclude_domains"] = blocked
    response = await client.post(
        "https://api.tavily.com/search", json=body, headers={"Authorization": f"Bearer {key}"}
    )
    response.raise_for_status()
    hits = response.json().get("results") or []
    return [SearchResult(h.get("title", ""), h.get("url", ""), h.get("content", "")) for h in hits]


async def _searxng(client: httpx.AsyncClient, base: str, query: str) -> list[SearchResult]:
    response = await client.get(f"{base.rstrip('/')}/search", params={"q": query, "format": "json"})
    response.raise_for_status()
    hits = response.json().get("results") or []
    return [SearchResult(h.get("title", ""), h.get("url", ""), h.get("content", "")) for h in hits]


def strip_tags(text: str) -> str:
    """Snippet text without the <strong> highlighting some APIs add."""
    return re.sub(r"<[^>]+>", "", text)


def filter_results(
    results: list[SearchResult], allowed: list[str], blocked: list[str], limit: int
) -> list[SearchResult]:
    """Apply domain filters (a domain also covers its subdomains) and drop duplicate URLs."""
    seen: set[str] = set()
    kept: list[SearchResult] = []
    for result in results:
        host = (urlsplit(result.url).hostname or "").lower()
        if allowed and not any(in_domain(host, d) for d in allowed):
            continue
        if any(in_domain(host, d) for d in blocked) or result.url in seen:
            continue
        seen.add(result.url)
        kept.append(result)
    return kept[:limit]


def in_domain(host: str, domain: str) -> bool:
    """True if `host` is `domain` or one of its subdomains."""
    domain = domain.lower().strip(".")
    return host == domain or host.endswith("." + domain)
