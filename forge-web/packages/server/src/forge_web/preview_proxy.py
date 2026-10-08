"""The preview proxy: requests for preview hosts go to a port inside the project's sandbox.

`PreviewRouter` wraps the whole app, so a request for a preview host never reaches Forge's own
routes, middleware or cookies. HTTP is relayed here, WebSockets in `preview_ws`.
"""

from collections.abc import AsyncIterator
from urllib.parse import parse_qs

import httpcore
from starlette.responses import PlainTextResponse, Response
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from forge_web.preview_auth import (
    COOKIE_NAME,
    COOKIE_SECONDS,
    ENTER_PATH,
    Grant,
    PreviewBase,
    Target,
    preview_base,
    preview_target,
)
from forge_web.preview_headers import cross_site_refused, downstream_headers, upstream_headers
from forge_web.preview_upstream import TIMEOUTS, NothingListening, SandboxDown, upstream
from forge_web.preview_ws import relay_websocket
from forge_web.services import Services

NOT_ALLOWED = "This preview is private. Open it from Forge (Preview tab)."


class PreviewRouter:
    """Sends requests for preview hosts to the preview proxy and everything else to the app."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Route one request."""
        # Before the app started (no services yet) nothing can be a preview.
        services: Services | None = getattr(scope["app"].state, "services", None)
        if scope["type"] in ("http", "websocket") and services is not None:
            base = preview_base(services.settings)
            target = preview_target(base, header(scope, b"host")) if base else None
            if base is not None and target is not None:
                proxy = Preview(services, base, target, scope)
                if scope["type"] == "http":
                    await proxy.http(receive, send)
                elif await proxy.authorized():
                    await relay_websocket(services, target, scope, receive, send)
                else:
                    await send({"type": "websocket.close", "code": 4403})
                return
        await self.app(scope, receive, send)


def header(scope: Scope, name: bytes) -> str | None:
    """One request header as text."""
    for key, value in scope.get("headers", []):
        if key.lower() == name:
            return bytes(value).decode("latin-1")
    return None


def cookies_named(scope: Scope, name: str) -> list[str]:
    """Every value of one cookie (another preview may have tossed in one of the same name)."""
    found = []
    for key, value in scope.get("headers", []):
        if key.lower() == b"cookie":
            for part in bytes(value).decode("latin-1").split(";"):
                key_text, _, cookie = part.strip().partition("=")
                if key_text == name:
                    found.append(cookie)
    return found


class Preview:
    """One request to one preview."""

    def __init__(self, services: Services, base: PreviewBase, target: Target, scope: Scope) -> None:
        self.services, self.base, self.target, self.scope = services, base, target, scope

    async def authorized(self) -> Grant | None:
        """The grant of a valid preview cookie (and, for WebSockets, our own Origin)."""
        if (
            self.scope["type"] == "websocket"
            and header(self.scope, b"origin") != self.target.origin
        ):
            return None
        access = self.services.previews
        for value in cookies_named(self.scope, COOKIE_NAME):
            grant = access.read_cookie(value, self.target)
            if grant is not None and await access.still_allowed(self.services.db, value, grant):
                return grant
        return None

    async def http(self, receive: Receive, send: Send) -> None:
        """Answer an HTTP request: the ticket exchange, a refusal, or the app's answer."""
        if self.scope["path"] == ENTER_PATH:
            response = self.enter()
        elif cross_site_refused(self.scope["method"], self.scope["headers"]):
            response = PlainTextResponse("Other sites may only link to a preview.", 403)
        elif await self.authorized() is None:
            response = PlainTextResponse(NOT_ALLOWED, 401)
        else:
            await forward_http(self.services, self.target, self.scope, receive, send)
            return
        await response(self.scope, receive, send)

    def enter(self) -> Response:
        """Turn a ticket into this preview's cookie, then show the page asked for."""
        query = parse_qs(self.scope.get("query_string", b"").decode("latin-1"))
        grant = self.services.previews.redeem(query.get("ticket", [""])[0], self.target)
        if self.scope["method"] != "GET" or grant is None:
            return PlainTextResponse(NOT_ALLOWED, 401)
        wanted = query.get("next", ["/"])[0]
        safe = wanted.startswith("/") and not wanted.startswith(("//", "/\\"))
        response = Response(status_code=303, headers={"Location": wanted if safe else "/",
                                                      "Cache-Control": "no-store"})  # fmt: skip
        value = self.services.previews.cookie_value(grant)
        response.headers.append(
            "set-cookie",
            f"{COOKIE_NAME}={value}; Path=/; Max-Age={COOKIE_SECONDS}; Secure; HttpOnly; "
            "SameSite=None; Partitioned",
        )
        return response


def frame_ancestors(services: Services) -> str:
    """Forge's own origins (the pages that may frame a preview)."""
    settings = services.settings
    found = [settings.base_url()] + [o.rstrip("/") for o in settings.auth.allowed_origins]
    if settings.dev.enabled:
        found += ["http://localhost:*", "http://127.0.0.1:*"]  # the Vite dev server
    return " ".join(dict.fromkeys(found))


async def request_body(receive: Receive, limit: int) -> AsyncIterator[bytes]:
    """The browser's request body, at most `limit` bytes."""
    total = 0
    while True:
        message = await receive()
        if message["type"] == "http.disconnect":
            raise ClientGone
        chunk = message.get("body", b"")
        total += len(chunk)
        if total > limit:
            raise BodyTooLarge
        if chunk:
            yield chunk
        if not message.get("more_body", False):
            return


class ClientGone(Exception):
    """The browser went away during the request."""


class BodyTooLarge(Exception):
    """The browser sent more than the upload limit."""


def target_path(scope: Scope) -> bytes:
    """The request target: raw path and query."""
    path = scope.get("raw_path") or scope["path"].encode("latin-1")
    query = scope.get("query_string", b"")
    return bytes(path) + (b"?" + query if query else b"")


async def forward_http(
    services: Services, target: Target, scope: Scope, receive: Receive, send: Send
) -> None:
    """Send the request to the app and stream its answer back."""
    headers = upstream_headers(scope["headers"], target)
    sent = dict(scope["headers"])
    if b"transfer-encoding" in sent and b"content-length" not in sent:
        headers.append((b"transfer-encoding", b"chunked"))
    has_body = b"content-length" in sent or b"transfer-encoding" in sent
    limit = services.settings.server.max_upload_mb * 1024 * 1024
    url = httpcore.URL(scheme=b"http", host=b"localhost", port=target.port,
                       target=target_path(scope))  # fmt: skip
    request = httpcore.Request(
        scope["method"], url, headers=headers,
        content=request_body(receive, limit) if has_body else b"",
        extensions={"timeout": TIMEOUTS},
    )  # fmt: skip
    connection = upstream(services, target)
    try:
        failure = await relay_response(services, target, connection, request, send)
    finally:
        await connection.aclose()
    if failure is not None:
        await failure(scope, receive, send)


async def relay_response(
    services: Services,
    target: Target,
    connection: httpcore.AsyncHTTPConnection,
    request: httpcore.Request,
    send: Send,
) -> Response | None:
    """Stream the app's answer to the browser; an error page instead if there is none."""
    try:
        response = await connection.handle_async_request(request)
    except NothingListening:
        return PlainTextResponse(f"Nothing listens on port {target.port} in this project. "
                                 "Start the dev server first.", 502)  # fmt: skip
    except SandboxDown:
        return PlainTextResponse("The project's sandbox is not reachable.", 503)
    except BodyTooLarge:
        return PlainTextResponse("The request is too large.", 413)
    except ClientGone:
        return None
    except (httpcore.TimeoutException, httpcore.NetworkError, httpcore.ProtocolError) as err:
        return PlainTextResponse(f"The app did not answer properly: {err}", 502)
    try:
        start: Message = {
            "type": "http.response.start", "status": response.status,
            "headers": downstream_headers(response.headers, target.port, frame_ancestors(services)),
        }  # fmt: skip
        await send(start)
        async for chunk in response.aiter_stream():
            await send({"type": "http.response.body", "body": chunk, "more_body": True})
        await send({"type": "http.response.body", "body": b"", "more_body": False})
    except (httpcore.NetworkError, httpcore.ProtocolError, httpcore.TimeoutException, OSError):
        pass  # the app or the browser went away mid-answer; nothing more to say
    finally:
        await response.aclose()
    return None
