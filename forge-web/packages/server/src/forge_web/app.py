"""The FastAPI application: one factory that wires settings into every route."""

from fastapi import APIRouter, FastAPI

from forge_web import __version__
from forge_web.settings import WebSettings


def create_app(settings: WebSettings) -> FastAPI:
    """A ready-to-serve app for these settings."""
    app = FastAPI(title="Forge Web", version=__version__, docs_url=None, redoc_url=None)
    app.state.settings = settings
    app.include_router(health_router())
    return app


def health_router() -> APIRouter:
    """`GET /api/health`: the server is up and which version it runs."""
    router = APIRouter()

    @router.get("/api/health")
    async def health() -> dict[str, object]:
        return {"ok": True, "version": __version__}

    return router
