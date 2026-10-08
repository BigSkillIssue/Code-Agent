"""Changing API requests from a browser must come from this site's own pages."""

from fastapi import Request
from fastapi.responses import JSONResponse, Response
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint

from forge_web.auth.sessions import UNSAFE, origin_allowed
from forge_web.services import services_of


class OriginGuard(BaseHTTPMiddleware):
    """Refuses cross-site POST/PUT/PATCH/DELETE to /api (sign-in included)."""

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        """Check the Origin header of changing API calls."""
        if request.method in UNSAFE and request.url.path.startswith("/api/"):
            origin = request.headers.get("origin")
            host = request.headers.get("host")
            if not origin_allowed(services_of(request), origin, host, request.url.scheme):
                return JSONResponse({"detail": "cross-site request refused"}, status_code=403)
        return await call_next(request)
