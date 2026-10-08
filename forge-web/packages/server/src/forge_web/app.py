"""The FastAPI application: one factory that wires settings into every part."""

import logging
import secrets
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import APIRouter, FastAPI
from sqlalchemy import update

from forge_web import __version__
from forge_web.auth.dev import dev_router, ensure_dev_user
from forge_web.chats.api import chats_router
from forge_web.chats.runs import RunManager
from forge_web.containers.driver import ContainerDriver
from forge_web.containers.local import LocalDriver
from forge_web.db.engine import Database
from forge_web.db.models import Chat
from forge_web.db.writer import EventWriter
from forge_web.fake import fake_script
from forge_web.hub import Hub
from forge_web.projects import projects_router
from forge_web.services import Services
from forge_web.settings import SettingsError, WebSettings
from forge_web.ws import ws_router

log = logging.getLogger(__name__)


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
        title="Forge Web", version=__version__, docs_url=None, redoc_url=None, lifespan=lifespan
    )
    for router in (health_router(), dev_router(), projects_router(), chats_router(), ws_router()):
        app.include_router(router)
    return app


def make_driver(settings: WebSettings) -> ContainerDriver:
    """The sandbox driver the settings ask for."""
    if settings.sandbox.isolation == "local":
        return LocalDriver(settings.data_dir)
    raise SettingsError("Docker isolation is not available yet; use local isolation (--dev)")


def chat_options(settings: WebSettings) -> Any:
    """A function giving the worker options of a chat."""
    script = fake_script(settings.dev.fake_script) if settings.dev.fake else None
    sandbox_mode = "workspace-write" if settings.sandbox.isolation == "local" else "full-access"

    def options_for(chat: Chat) -> dict[str, Any]:
        options: dict[str, Any] = {"mode": chat.mode, "sandbox_mode": sandbox_mode}
        if chat.model:
            options["model"] = chat.model
        if script is not None:
            options["fake_script"] = script
        return options

    return options_for


async def start_services(settings: WebSettings, driver: ContainerDriver | None) -> Services:
    """Open the database, start the writer and connect the parts."""
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    db = Database(settings.database_url())
    await db.migrate()
    writer = EventWriter(db)
    writer.start()
    hub = Hub()
    chosen = driver or make_driver(settings)
    runs = RunManager(db, writer, chosen, hub, chat_options(settings))
    services = Services(settings=settings, db=db, writer=writer, driver=chosen, hub=hub, runs=runs)
    if chosen.name == "local":
        # Local sandboxes end with the server, so nothing can still be running.
        async with db.session() as session, session.begin():
            await session.execute(update(Chat).where(Chat.state != "idle").values(state="idle"))
    if settings.dev.enabled:
        user = await ensure_dev_user(db)
        services.dev_token, services.dev_user_id = secrets.token_urlsafe(24), user.id
        link = f"{settings.base_url()}/api/auth/dev-login?token={services.dev_token}"
        print(f"\nForge Web (development mode): open {link}\n", file=sys.stderr, flush=True)
    return services


async def stop_services(services: Services) -> None:
    """Close everything in reverse order."""
    await services.runs.close()
    await services.writer.close()
    await services.db.close()


def health_router() -> APIRouter:
    """`GET /api/health`: the server is up and which version it runs."""
    router = APIRouter()

    @router.get("/api/health")
    async def health() -> dict[str, object]:
        return {"ok": True, "version": __version__}

    return router
