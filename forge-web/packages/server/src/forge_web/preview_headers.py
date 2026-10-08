"""Headers between the browser and an app in a preview, both ways.

To the app the request looks local: Host and Origin are `localhost:<port>` (dev servers such as
Vite refuse other hosts), and Forge's own cookies and the reverse proxy's X-Forwarded headers
are left out. From the app, cookies lose any Domain (so a preview cannot set cookies for other
previews), cookies named like Forge's are dropped, local redirects become relative, and only
Forge's pages may frame the preview.
"""

from collections.abc import Iterable
from urllib.parse import urlsplit

from forge_web.preview_auth import COOKIE_NAME, Target

Headers = list[tuple[bytes, bytes]]

HOP_BY_HOP = frozenset({
    b"connection", b"keep-alive", b"proxy-authenticate", b"proxy-authorization",
    b"proxy-connection", b"te", b"trailer", b"transfer-encoding", b"upgrade",
})  # fmt: skip
NOT_FORWARDED = frozenset({
    b"host", b"forwarded", b"x-forwarded-for", b"x-forwarded-host", b"x-forwarded-proto",
    b"x-forwarded-port", b"x-real-ip",
})  # fmt: skip
FORGE_COOKIES = frozenset({
    COOKIE_NAME, "forge_preview", "forge_session", "__Host-forge_session", "forge_csrf",
})  # fmt: skip


def upstream_headers(headers: Iterable[tuple[bytes, bytes]], target: Target, *,
                     websocket: bool = False) -> Headers:  # fmt: skip
    """The browser's request headers as the app in the sandbox gets them."""
    local = f"localhost:{target.port}".encode()
    out: Headers = [(b"host", local)]
    for raw_name, value in headers:
        name = raw_name.lower()
        if name in HOP_BY_HOP or name in NOT_FORWARDED:
            continue
        if websocket and name.startswith(b"sec-websocket-"):
            continue  # the proxy makes its own handshake with the app
        if name == b"cookie":
            value = strip_forge_cookies(value)
        elif name in (b"origin", b"referer"):
            value = local_url(value, target, local)
        if value:
            out.append((name, value))
    return out


def strip_forge_cookies(value: bytes) -> bytes:
    """A Cookie header without Forge's cookies."""
    kept = []
    for part in value.decode("latin-1").split(";"):
        name = part.split("=", 1)[0].strip()
        if name and name not in FORGE_COOKIES:
            kept.append(part.strip())
    return "; ".join(kept).encode("latin-1")


def local_url(value: bytes, target: Target, local: bytes) -> bytes:
    """An Origin or Referer of the preview itself, pointed at localhost; empty for others."""
    text = value.decode("latin-1")
    parts = urlsplit(text)
    if f"{parts.scheme}://{parts.netloc}".lower() != target.origin:
        return b""
    rest = text[len(parts.scheme) + 3 + len(parts.netloc) :]
    return b"http://" + local + rest.encode("latin-1")


def downstream_headers(headers: Iterable[tuple[bytes, bytes]], port: int,
                       frame_ancestors: str) -> Headers:  # fmt: skip
    """The app's response headers as the browser gets them."""
    out: Headers = []
    for raw_name, value in headers:
        name = raw_name.lower()
        if name in HOP_BY_HOP:
            continue
        if name == b"set-cookie":
            cleaned = clean_set_cookie(value)
            if cleaned is None:
                continue
            value = cleaned
        elif name == b"location":
            value = relative_location(value, port)
        out.append((name, value))
    out.append((b"content-security-policy", f"frame-ancestors {frame_ancestors}".encode()))
    return out


def clean_set_cookie(value: bytes) -> bytes | None:
    """A Set-Cookie for this host only; None for cookies named like Forge's."""
    parts = [p.strip() for p in value.decode("latin-1").split(";")]
    name = parts[0].split("=", 1)[0].strip()
    if not name or name in FORGE_COOKIES:
        return None
    kept = [parts[0]] + [p for p in parts[1:] if p and not p.lower().startswith("domain")]
    return "; ".join(kept).encode("latin-1")


def relative_location(value: bytes, port: int) -> bytes:
    """A redirect to the app's own local address, as a path; others stay as they are."""
    text = value.decode("latin-1")
    for host in ("localhost", "127.0.0.1", "[::1]"):
        for scheme in ("http", "https"):
            prefix = f"{scheme}://{host}:{port}"
            if text.lower().startswith(prefix) and text[len(prefix) : len(prefix) + 1] in (
                "",
                "/",
                "?",
            ):
                rest = text[len(prefix) :]
                return (rest if rest.startswith("/") else "/" + rest).encode("latin-1")
    return value


def cross_site_refused(method: str, headers: Iterable[tuple[bytes, bytes]]) -> bool:
    """Another site (or another preview) asks for more than a plain navigation."""
    found = {name.lower(): value for name, value in headers}
    site = found.get(b"sec-fetch-site")
    if site is None or site in (b"same-origin", b"none"):
        return False  # our own pages, a typed address, or a browser without fetch metadata
    return not (found.get(b"sec-fetch-mode") == b"navigate" and method in ("GET", "HEAD"))
