"""The built web UI: files from `static/`, `index.html` for every app route, safe headers.

`/api/*` never falls back to the app, so an unknown API path is a plain 404.
"""

from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, HTMLResponse, Response
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint

STATIC = Path(__file__).parent / "static"
NOT_BUILT = """<!doctype html><meta charset="utf-8"><title>Forge</title>
<body style="font-family:system-ui;max-width:40rem;margin:4rem auto;padding:0 1rem">
<h1>The web UI is not built yet</h1>
<p>Build it once: <code>cd forge-web/frontend &amp;&amp; npm ci &amp;&amp; npm run build</code>,
then reload this page.</p></body>"""
CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data: blob:; font-src 'self' data:; connect-src 'self' ws: wss:; "
    "frame-ancestors 'none'; base-uri 'none'; form-action 'self'; object-src 'none'"
)
HEADERS = {
    "Content-Security-Policy": CSP,
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "same-origin",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
    "Cross-Origin-Opener-Policy": "same-origin",
}


class SecurityHeaders(BaseHTTPMiddleware):
    """Adds the headers above to every response (HSTS too when served over HTTPS)."""

    def __init__(self, app: object, *, https: bool, frame_src: str = "") -> None:
        super().__init__(app)  # type: ignore[arg-type]
        self.https = https
        # Forge's pages may frame live previews (their own hosts), nothing else.
        self.headers = {**HEADERS, "Content-Security-Policy": f"{CSP}; frame-src {frame_src}"
                        if frame_src else CSP}  # fmt: skip

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        """Add the headers."""
        response = await call_next(request)
        for name, value in self.headers.items():
            response.headers.setdefault(name, value)
        if self.https:
            response.headers.setdefault("Strict-Transport-Security", "max-age=31536000")
        return response


def static_file(path: str, root: Path = STATIC) -> Path | None:
    """A built file for a URL path, or None (never anything outside `root`)."""
    candidate = (root / path).resolve()
    if candidate.is_file() and root.resolve() in candidate.parents:
        return candidate
    return None


def mount_web_ui(app: FastAPI, root: Path = STATIC) -> None:
    """Serve the built app for every path that is not an API path."""

    @app.get("/{path:path}", include_in_schema=False)
    async def web_ui(path: str) -> Response:
        if path == "api" or path.startswith("api/"):
            return Response(status_code=404)
        found = static_file(path, root) if path else None
        if found is not None:
            cache = (
                "public, max-age=31536000, immutable" if path.startswith("assets/") else "no-cache"
            )
            return FileResponse(found, headers={"Cache-Control": cache})
        index = root / "index.html"
        if index.is_file():
            return FileResponse(index, headers={"Cache-Control": "no-cache"})
        return HTMLResponse(NOT_BUILT)
