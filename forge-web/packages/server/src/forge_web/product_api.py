"""What Forge's agent made of a project (W55), for the project panel's "Product" tab: whether it
is a product, which clients it has, where it runs on Forge's servers and how its newest deploys
went. People only look here: the agent decides and builds (forge_sandbox.product_flow)."""

from typing import Any

from fastapi import APIRouter, HTTPException, Request
from forge.app_manifest import parse_manifest
from sqlalchemy import select

from forge_web.auth.sessions import CurrentUser
from forge_web.db.hosting_models import Deploy, HostedApp
from forge_web.files_api import allowed
from forge_web.hosting.deploy import view
from forge_web.sandbox_calls import result_dict, sandbox_call
from forge_web.services import Services

SHOWN = 5


async def manifest_text(services: Services, project_id: str) -> str | None:
    """The project's forge.app.toml, if it has one."""
    try:
        read = result_dict(
            await sandbox_call(services, project_id, "fs.read", {"path": "forge.app.toml"})
        )
    except HTTPException:
        return None
    text = read.get("text")
    return text if isinstance(text, str) else None


def product_router() -> APIRouter:
    """`/api/projects/{id}/product`."""
    router = APIRouter(prefix="/api/projects/{project_id}")

    @router.get("/product")
    async def product(project_id: str, request: Request, user: CurrentUser) -> dict[str, Any]:
        services = await allowed(request, user, project_id)
        text = await manifest_text(services, project_id)
        manifest = parse_manifest(text) if text is not None else None
        if manifest is None or isinstance(manifest, list):
            return {"product": False}
        async with services.db.session() as session:
            hosted = await session.get(HostedApp, project_id)
            deploys = [view(row) for row in await session.scalars(
                select(Deploy).where(Deploy.project_id == project_id)
                .order_by(Deploy.created_at.desc()).limit(SHOWN))]  # fmt: skip
        live = {d["environment"]: d["url"] for d in reversed(deploys) if d["status"] == "live"}
        static = all(service.runtime == "static" for service in manifest.services)
        return {
            "product": True, "name": manifest.name,
            "kind": "site" if static else "fullstack",
            "clients": list(manifest.clients), "payments": manifest.payments.kind == "relay",
            "database": manifest.database is not None, "hosted": hosted is not None,
            "staging_url": live.get("staging", ""), "production_url": live.get("production", ""),
            "deploys": [{k: d[k] for k in ("environment", "status", "step", "commit", "error",
                                           "created_at")} for d in deploys],
        }  # fmt: skip

    return router
