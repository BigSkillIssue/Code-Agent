"""Signed-in browsers: server-side sessions, the cookies that name them, and CSRF protection.

The session cookie holds a random token (HttpOnly); the database keeps only its SHA-256. Every
request that changes something must also send the session's CSRF value in `X-CSRF-Token`; the
value sits in a second cookie that only scripts of this site can read. Requests from browsers
must come from this site's own origin (checked for API calls and WebSockets).
"""

import hashlib
import secrets
import time
from typing import Annotated
from urllib.parse import urlsplit

from fastapi import Depends, HTTPException, Request, Response, WebSocket
from sqlalchemy import delete

from forge_web.db.models import AuthSession, User
from forge_web.services import Services, services_of

SESSION_COOKIE = "forge_session"
SECURE_SESSION_COOKIE = "__Host-forge_session"
CSRF_COOKIE = "forge_csrf"
CSRF_HEADER = "x-csrf-token"
UNSAFE = frozenset({"POST", "PUT", "PATCH", "DELETE"})
TOUCH_EVERY = 300.0  # seconds between last-seen updates


def token_id(token: str) -> str:
    """What the database stores for a session token."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def is_https(services: Services) -> bool:
    """The server is reached over HTTPS."""
    return services.settings.base_url().startswith("https://")


def client_ip(request: Request | WebSocket) -> str:
    """The caller's address (the reverse proxy's X-Forwarded-For is trusted by uvicorn)."""
    return request.client.host if request.client is not None else ""


async def start_session(request: Request, response: Response, user: User) -> AuthSession:
    """Sign the user in: a new session and its cookies."""
    services = services_of(request)
    token, now = secrets.token_urlsafe(32), time.time()
    days = services.settings.auth.session_days
    session_row = AuthSession(
        id=token_id(token), user_id=user.id, csrf=secrets.token_urlsafe(24), created_at=now,
        last_seen_at=now, expires_at=now + days * 86400, ip=client_ip(request),
        user_agent=request.headers.get("user-agent", "")[:300],
    )  # fmt: skip
    async with services.db.session() as session, session.begin():
        session.add(session_row)
    secure = is_https(services)
    response.set_cookie(
        SECURE_SESSION_COOKIE if secure else SESSION_COOKIE, token, max_age=int(days * 86400),
        httponly=True, secure=secure, samesite="lax", path="/",
    )  # fmt: skip
    response.set_cookie(
        CSRF_COOKIE, session_row.csrf, max_age=int(days * 86400),
        httponly=False, secure=secure, samesite="strict", path="/",
    )  # fmt: skip
    return session_row


def clear_cookies(response: Response) -> None:
    """Remove the session cookies from the browser."""
    for name in (SESSION_COOKIE, SECURE_SESSION_COOKIE, CSRF_COOKIE):
        response.delete_cookie(name, path="/")


def session_token(cookies: dict[str, str]) -> str | None:
    """The session token from either cookie name."""
    return cookies.get(SECURE_SESSION_COOKIE) or cookies.get(SESSION_COOKIE)


async def load_session(services: Services, token: str | None) -> tuple[AuthSession, User] | None:
    """The live session and its active user, or None."""
    if not token:
        return None
    now = time.time()
    async with services.db.session() as session, session.begin():
        row = await session.get(AuthSession, token_id(token))
        if row is None or row.expires_at < now:
            return None
        user = await session.get(User, row.user_id)
        if user is None or user.status != "active":
            return None
        if now - row.last_seen_at > TOUCH_EVERY:
            row.last_seen_at = now
        return row, user


async def end_sessions(services: Services, user_id: str, *, keep: str | None = None) -> None:
    """Sign a user out everywhere (except the session `keep`)."""
    async with services.db.session() as session, session.begin():
        query = delete(AuthSession).where(AuthSession.user_id == user_id)
        if keep is not None:
            query = query.where(AuthSession.id != keep)
        await session.execute(query)


def origin_allowed(services: Services, origin: str | None, host: str | None, scheme: str) -> bool:
    """A browser request comes from this site (requests without Origin are not from browsers)."""
    if not origin:
        return True
    allowed = {f"{scheme}://{host}"} if host else set()
    if services.settings.server.public_url:
        parts = urlsplit(services.settings.base_url())
        allowed.add(f"{parts.scheme}://{parts.netloc}")
    allowed.update(o.rstrip("/") for o in services.settings.auth.allowed_origins)
    if services.settings.dev.enabled and urlsplit(origin).hostname in ("localhost", "127.0.0.1"):
        return True  # the Vite dev server
    return origin.rstrip("/") in allowed


async def current_user(request: Request) -> User:
    """The signed-in user; 401 otherwise. Changing requests must carry the CSRF value."""
    services = services_of(request)
    found = await load_session(services, session_token(request.cookies))
    if found is None:
        raise HTTPException(401, "sign in first")
    row, user = found
    if request.method in UNSAFE:
        sent = request.headers.get(CSRF_HEADER, "")
        if not sent or not secrets.compare_digest(sent, row.csrf):
            raise HTTPException(403, "missing or wrong CSRF token")
    request.state.session_id = row.id
    return user


CurrentUser = Annotated[User, Depends(current_user)]


async def require_admin(request: Request, user: CurrentUser) -> User:
    """The signed-in user, who must be an admin (with two-factor on if the server says so)."""
    if user.role != "admin":
        raise HTTPException(403, "only admins can do that")
    if services_of(request).settings.auth.admin_two_factor and not user.totp_enabled:
        raise HTTPException(
            403, "this server requires two-factor sign-in for admins: turn it on in your settings"
        )
    return user


AdminUser = Annotated[User, Depends(require_admin)]


async def websocket_user(websocket: WebSocket) -> User | None:
    """The signed-in user of a WebSocket from this site, or None."""
    services = services_of(websocket)
    origin = websocket.headers.get("origin")
    if not origin_allowed(
        services, origin, websocket.headers.get("host"), _http(websocket.url.scheme)
    ):
        return None
    found = await load_session(services, session_token(websocket.cookies))
    return found[1] if found is not None else None


def _http(scheme: str) -> str:
    return {"ws": "http", "wss": "https"}.get(scheme, scheme)
