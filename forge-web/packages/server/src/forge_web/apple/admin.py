"""Admin endpoints for Apple builds (`/api/admin/apple/...`): Macs and their tokens, the job queue,
and who may build. Settings (on/off, who, minutes) go through the general admin settings."""

import time
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import delete, select

from forge_web.apple.workers import ONLINE_S, create_worker
from forge_web.audit import audit
from forge_web.auth.sessions import AdminUser, client_ip
from forge_web.db.models import AppleJob, MacWorker, Project, User
from forge_web.services import services_of


class NewMac(BaseModel):
    """A Mac to add."""

    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=200)


class MacChange(BaseModel):
    """Turn a Mac on or off."""

    model_config = ConfigDict(extra="forbid")
    enabled: bool


class Grant(BaseModel):
    """Whether a user may build Apple apps (when the server allows it per user)."""

    model_config = ConfigDict(extra="forbid")
    allowed: bool


def mac_info(row: MacWorker, now: float) -> dict[str, Any]:
    """A Mac as the admin page shows it (never its token)."""
    return {"id": row.id, "name": row.name, "enabled": row.enabled, "created_at": row.created_at,
            "last_seen": row.last_seen, "online": now - row.last_seen < ONLINE_S,
            "version": row.version}  # fmt: skip


def apple_admin_router() -> APIRouter:
    """Routes for admins."""
    router = APIRouter(prefix="/api/admin/apple")

    @router.get("/macs")
    async def macs(request: Request, admin: AdminUser) -> list[dict[str, Any]]:
        async with services_of(request).db.session() as session:
            rows = list(await session.scalars(select(MacWorker).order_by(MacWorker.created_at)))
        return [mac_info(row, time.time()) for row in rows]

    @router.post("/macs")
    async def add_mac(body: NewMac, request: Request, admin: AdminUser) -> dict[str, Any]:
        services = services_of(request)
        row, token = await create_worker(services.db, body.name)
        await audit(services.db, "apple.mac_added", user_id=admin.id, target=row.id,
                    ip=client_ip(request), name=body.name)  # fmt: skip
        return {**mac_info(row, time.time()), "token": token}  # shown this once only

    @router.patch("/macs/{mac_id}")
    async def change_mac(
        mac_id: str, body: MacChange, request: Request, admin: AdminUser
    ) -> dict[str, Any]:
        services = services_of(request)
        async with services.db.session() as session, session.begin():
            row = await session.get(MacWorker, mac_id)
            if row is None:
                raise HTTPException(404, "no such Mac")
            row.enabled = body.enabled
        action = "apple.mac_enabled" if body.enabled else "apple.mac_disabled"
        await audit(services.db, action, user_id=admin.id, target=mac_id, ip=client_ip(request))
        return mac_info(row, time.time())

    @router.delete("/macs/{mac_id}")
    async def remove_mac(mac_id: str, request: Request, admin: AdminUser) -> dict[str, bool]:
        services = services_of(request)
        async with services.db.session() as session, session.begin():
            gone = await session.execute(delete(MacWorker).where(MacWorker.id == mac_id))
        if not gone.rowcount:  # type: ignore[attr-defined]
            raise HTTPException(404, "no such Mac")
        await audit(services.db, "apple.mac_removed", user_id=admin.id, target=mac_id,
                    ip=client_ip(request))  # fmt: skip
        return {"ok": True}

    @router.get("/jobs")
    async def jobs(request: Request, admin: AdminUser, limit: int = 100) -> list[dict[str, Any]]:
        return await recent_jobs(request, min(max(limit, 1), 500))

    @router.put("/users/{user_id}")
    async def grant(
        user_id: str, body: Grant, request: Request, admin: AdminUser
    ) -> dict[str, Any]:
        services = services_of(request)
        async with services.db.session() as session, session.begin():
            user = await session.get(User, user_id)
            if user is None:
                raise HTTPException(404, "no such user")
            user.apple_allowed = body.allowed
        action = "apple.granted" if body.allowed else "apple.revoked"
        await audit(services.db, action, user_id=admin.id, target=user_id, ip=client_ip(request))
        minutes = await services.apple.minutes_used(user_id)
        return {"user_id": user_id, "allowed": body.allowed, "minutes_this_month": minutes}

    return router


async def recent_jobs(request: Request, limit: int) -> list[dict[str, Any]]:
    """The newest jobs with who asked, for which project, and how they went."""
    async with services_of(request).db.session() as session:
        rows = await session.execute(
            select(AppleJob, User.email, Project.name)
            .join(User, User.id == AppleJob.user_id)
            .join(Project, Project.id == AppleJob.project_id)
            .order_by(AppleJob.created_at.desc()).limit(limit)
        )  # fmt: skip
        return [
            {"id": job.id, "kind": job.kind, "params": job.params, "status": job.status,
             "user": email or "", "project": name, "worker_id": job.worker_id,
             "created_at": job.created_at, "seconds": job.seconds, "outcome": job.outcome}
            for job, email, name in rows
        ]  # fmt: skip
