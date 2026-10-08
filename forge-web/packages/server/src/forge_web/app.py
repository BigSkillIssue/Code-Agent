"""The FastAPI application: one factory that wires settings into every part."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import APIRouter, FastAPI

from forge_web import __version__
from forge_web.auth.admin import admin_router
from forge_web.auth.dev import dev_router
from forge_web.auth.git_credentials import git_credentials_router
from forge_web.auth.oauth import oauth_router
from forge_web.auth.origin import OriginGuard
from forge_web.auth.routes import auth_router
from forge_web.chats.api import chats_router
from forge_web.containers.driver import ContainerDriver
from forge_web.files_api import files_router
from forge_web.gateway.api import keys_router
from forge_web.git_api import git_router
from forge_web.members import members_router
from forge_web.preview import preview_router
from forge_web.preview_auth import preview_base
from forge_web.preview_proxy import PreviewRouter
from forge_web.projects import projects_router
from forge_web.settings import WebSettings
from forge_web.sources import sources_router
from forge_web.startup import start_services, stop_services
from forge_web.terminals import terminals_router
from forge_web.webui import SecurityHeaders, mount_web_ui
from forge_web.ws import ws_router


def create_app(settings: WebSettings, *, driver: ContainerDriver | None = None) -> FastAPI:
    """A ready-to-serve app for these settings (tests may pass their own sandbox driver)."""

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        services = await start_services(settings, driver)
        app.state.services = services
        try:
            yield
        finally:
            await stop_services(services)

    app = FastAPI(
        title="Forge Web", version=__version__, lifespan=lifespan,
        docs_url=None, redoc_url=None, openapi_url=None,
    )  # fmt: skip
    routers = (
        health_router(), auth_router(), oauth_router(), admin_router(), dev_router(),
        projects_router(), members_router(), chats_router(), files_router(), git_router(),
        sources_router(), terminals_router(), preview_router(), keys_router(),
        git_credentials_router(),
    )  # fmt: skip
    for router in (*routers, ws_router()):
        app.include_router(router)
    app.add_middleware(OriginGuard)
    base = preview_base(settings)
    app.add_middleware(
        SecurityHeaders, https=settings.base_url().startswith("https://"),
        frame_src=base.frame_source() if base else "",
    )  # fmt: skip
    app.add_middleware(PreviewRouter)  # outermost: preview hosts never reach Forge's routes
    mount_web_ui(app)  # last: it answers every path the API does not
    return app


def health_router() -> APIRouter:
    """`GET /api/health`: the server is up and which version it runs."""
    router = APIRouter()

    @router.get("/api/health")
    async def health() -> dict[str, object]:
        return {"ok": True, "version": __version__}

    return router
