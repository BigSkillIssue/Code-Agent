"""Changing API requests from a browser must come from this site's own pages."""

from fastapi import Request
from fastapi.responses import JSONResponse, Response
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint

from forge_web.auth.sessions import UNSAFE, origin_allowed
from forge_web.services import services_of


def apple_answer(request: Request) -> bool:
    """Sign in with Apple posting its answer from appleid.apple.com: the handler only redirects
    to the same callback as a GET, which then checks the attempt (see `auth.oauth`)."""
    parts = request.url.path.split("/")
    return (
        request.method == "POST"
        and len(parts) == 6
        and parts[1:4] == ["api", "auth", "oauth"]
        and parts[5] == "callback"
        and request.headers.get("origin") == "https://appleid.apple.com"
    )


class OriginGuard(BaseHTTPMiddleware):
    """Refuses cross-site POST/PUT/PATCH/DELETE to /api (sign-in included)."""

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        """Check the Origin header of changing API calls."""
        if (
            request.method in UNSAFE
            and request.url.path.startswith("/api/")
            and not apple_answer(request)
        ):
            origin = request.headers.get("origin")
            host = request.headers.get("host")
            if not origin_allowed(services_of(request), origin, host, request.url.scheme):
                return JSONResponse({"detail": "cross-site request refused"}, status_code=403)
        return await call_next(request)
