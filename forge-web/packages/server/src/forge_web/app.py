"""The FastAPI application: one factory that wires settings into every part."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import APIRouter, FastAPI

from forge_web import __version__
from forge_web.admin_api import admin_api_router
from forge_web.apple.admin import apple_admin_router
from forge_web.apple.approvals import approvals_router
from forge_web.apple.asc_keys import asc_key_routes
from forge_web.apple.iap import iap_router
from forge_web.apple.listing import listing_routes
from forge_web.apple.release_api import release_routes
from forge_web.apple.screens_api import screens_router
from forge_web.apple.submit_api import submission_routes
from forge_web.apple.worker_api import worker_router
from forge_web.auth.admin import admin_router
from forge_web.auth.dev import dev_router
from forge_web.auth.git_credentials import git_credentials_router
from forge_web.auth.oauth import oauth_router
from forge_web.auth.origin import OriginGuard
from forge_web.auth.routes import auth_router
from forge_web.auth.second_factor import second_factor_router
from forge_web.billing.admin import billing_admin_router
from forge_web.billing.cancel import cancel_router
from forge_web.billing.checkout import checkout_router
from forge_web.billing.connect import connect_router
from forge_web.billing.relay import payments_router
from forge_web.billing.webhooks import webhook_router
from forge_web.chats.api import chats_router
from forge_web.containers.driver import ContainerDriver
from forge_web.files_api import files_router
from forge_web.gateway.api import keys_router
from forge_web.git_api import git_router
from forge_web.hosting.abuse import notices_router, report_router
from forge_web.hosting.admin import hosting_admin_router
from forge_web.hosting.deploy_api import deploy_router, host_router
from forge_web.hosting.domains import domains_router
from forge_web.hosting.gdpr import gdpr_router
from forge_web.hosting.legal_pages import legal_router, operator_router
from forge_web.hosting.mail_relay import relay_router
from forge_web.legal import legal_texts_router
from forge_web.members import members_router
from forge_web.preview import preview_hosts_router, preview_router
from forge_web.preview_auth import preview_base
from forge_web.preview_proxy import PreviewRouter
from forge_web.projects import projects_router
from forge_web.settings import WebSettings
from forge_web.settings_api import settings_router
from forge_web.sources import sources_router
from forge_web.startup import start_services, stop_services
from forge_web.stores.android_api import android_router
from forge_web.stores.play_api import play_router
from forge_web.stores.play_keys import play_key_routes
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
        health_router(), auth_router(), second_factor_router(), oauth_router(), admin_router(),
        admin_api_router(), settings_router(), dev_router(),
        projects_router(), members_router(), chats_router(), files_router(), git_router(),
        sources_router(), terminals_router(), preview_router(), preview_hosts_router(),
        keys_router(), apple_admin_router(), worker_router(), screens_router(),
        approvals_router(), asc_key_routes(), release_routes(), listing_routes(),
        submission_routes(),
        git_credentials_router(), host_router(), deploy_router(), hosting_admin_router(),
        operator_router(), legal_router(), relay_router(), report_router(), notices_router(),
        gdpr_router(), domains_router(), billing_admin_router(), webhook_router(),
        checkout_router(), cancel_router(), legal_texts_router(), connect_router(),
        payments_router(), iap_router(), android_router(), play_router(), play_key_routes(),
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
