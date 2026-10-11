"""Hosting administration (W27), `/api/admin/hosting/...`: the hosts and their one-time tokens;
the production deploys waiting for an admin, each with exactly the commit the host checked and
its checks; the hosted apps (suspend, resume, read their logs); and who is trusted (their
production deploys need no admin). Every change is in the audit log.
"""

import asyncio
import json
import logging
import secrets
import time
from collections.abc import Awaitable
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from forge_hostworker.wire import HostJob
from forge_web.audit import audit
from forge_web.auth.sessions import AdminUser, client_ip
from forge_web.db.hosting_models import AppBackup, Deploy, GoLiveApproval, HostedApp, HostServer
from forge_web.db.models import Project, User
from forge_web.hosting.deploy import view
from forge_web.hosting.hosts import ONLINE_S, create_host
from forge_web.services import Services, services_of

log = logging.getLogger(__name__)
LOGS_WAIT_S = 60.0
ADMIN_JOB_TIMEOUT_S = 300.0


class NewHost(BaseModel):
    """A host's name and the public key `forge-host-worker keygen` printed."""

    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=200)
    public_key: str = Field(min_length=40, max_length=100)


class HostChange(BaseModel):
    """Turn a host on or off (an off host gets no jobs and is refused)."""

    model_config = ConfigDict(extra="forbid")
    enabled: bool


class Refusal(BaseModel):
    """Why an admin refuses a production deploy (the creator reads it)."""

    model_config = ConfigDict(extra="forbid")
    reason: str = Field(min_length=1, max_length=2_000)


class Suspension(BaseModel):
    """Suspend an app (taken off the edge, its containers stopped) or let it run again."""

    model_config = ConfigDict(extra="forbid")
    suspended: bool
    reason: str = Field(default="", max_length=2_000)


class LogsIn(BaseModel):
    """Which environment's logs."""

    model_config = ConfigDict(extra="forbid")
    environment: Literal["staging", "production"]


class TrustIn(BaseModel):
    """Whether a user's production deploys need an admin."""

    model_config = ConfigDict(extra="forbid")
    trusted: bool


def host_view(row: HostServer, apps: int) -> dict[str, Any]:
    """A host as the panel shows it."""
    return {"id": row.id, "name": row.name, "enabled": row.enabled, "created_at": row.created_at,
            "last_seen": row.last_seen, "online": time.time() - row.last_seen < ONLINE_S,
            "version": row.version, "apps": apps}  # fmt: skip


def hosting_admin_router() -> APIRouter:
    """The hosting panel's routes (admins only)."""
    router = APIRouter(prefix="/api/admin/hosting")
    host_routes(router)
    queue_routes(router)
    app_routes(router)
    user_routes(router)
    return router


def host_routes(router: APIRouter) -> None:
    @router.get("/hosts")
    async def hosts(request: Request, admin: AdminUser) -> list[dict[str, Any]]:
        services = services_of(request)
        async with services.db.session() as session:
            rows = list(await session.scalars(select(HostServer).order_by(HostServer.created_at)))
            apps = list(await session.scalars(select(HostedApp.host_id)))
        return [host_view(row, apps.count(row.id)) for row in rows]

    @router.post("/hosts")
    async def add_host(body: NewHost, request: Request, admin: AdminUser) -> dict[str, Any]:
        services = services_of(request)
        try:
            row, token = await create_host(services.db, body.name.strip(), body.public_key.strip())
        except ValueError as err:
            raise HTTPException(422, str(err)) from None
        await audit(services.db, "hosting.host_added", user_id=admin.id, target=row.id,
                    ip=client_ip(request), name=row.name)  # fmt: skip
        return {**host_view(row, 0), "token": token}  # the token is shown this once

    @router.patch("/hosts/{host_id}")
    async def change_host(
        host_id: str, body: HostChange, request: Request, admin: AdminUser
    ) -> dict[str, Any]:
        services = services_of(request)
        async with services.db.session() as session, session.begin():
            row = await session.get(HostServer, host_id)
            if row is None:
                raise HTTPException(404, "no such host")
            row.enabled = body.enabled
        await audit(services.db, "hosting.host_changed", user_id=admin.id, target=host_id,
                    ip=client_ip(request), enabled=body.enabled)  # fmt: skip
        return host_view(row, 0)


def queue_routes(router: APIRouter) -> None:
    @router.get("/queue")
    async def queue(request: Request, admin: AdminUser) -> list[dict[str, Any]]:
        services = services_of(request)
        async with services.db.session() as session:
            rows = await session.execute(
                select(Deploy, Project.name, User.name, User.email)
                .join(Project, Project.id == Deploy.project_id)
                .join(User, User.id == Deploy.user_id)
                .where(Deploy.status == "waiting", Deploy.step == "admin")
                .order_by(Deploy.created_at)
            )  # fmt: skip
            items = []
            for row, project, name, email in rows:
                approved = select(GoLiveApproval.summary).where(
                    GoLiveApproval.project_id == row.project_id,
                    GoLiveApproval.commit == row.commit,
                )
                summary = await session.scalar(
                    approved.order_by(GoLiveApproval.created_at.desc()).limit(1))  # fmt: skip
                items.append({**view(row), "project_id": row.project_id, "project": project,
                              "creator": name or email or row.user_id,
                              "review_summary": summary or ""})  # fmt: skip
        return items

    @router.post("/deploys/{deploy_id}/approve")
    async def approve(deploy_id: str, request: Request, admin: AdminUser) -> dict[str, Any]:
        services = services_of(request)
        row = await decided(services, deploy_id, admin, refusal="")
        await audit(services.db, "hosting.deploy_approved", user_id=admin.id, target=row.project_id,
                    ip=client_ip(request), deploy=deploy_id, commit=row.commit)  # fmt: skip
        services.deploys.launch(deploy_id)
        return view(row)

    @router.post("/deploys/{deploy_id}/refuse")
    async def refuse(
        deploy_id: str, body: Refusal, request: Request, admin: AdminUser
    ) -> dict[str, Any]:
        services = services_of(request)
        row = await decided(services, deploy_id, admin, refusal=body.reason.strip())
        await audit(services.db, "hosting.deploy_refused", user_id=admin.id, target=row.project_id,
                    ip=client_ip(request), deploy=deploy_id, reason=body.reason[:500])  # fmt: skip
        return view(row)


async def decided(services: Services, deploy_id: str, admin: User, refusal: str) -> Deploy:
    """A production deploy that waited for an admin, approved (no refusal) or refused."""
    async with services.db.session() as session, session.begin():
        row = await session.get(Deploy, deploy_id)
        if row is None:
            raise HTTPException(404, "no such deploy")
        if (row.status, row.step) != ("waiting", "admin"):
            raise HTTPException(409, "this deploy does not wait for an admin")
        row.admin_id, row.updated_at = admin.id, time.time()
        if refusal:
            row.status, row.error = "failed", f"an admin refused it: {refusal}"
            row.hint = "change what the admin asks for, then ask Forge to make it ready again"
        else:
            row.admin_approved_at, row.status = time.time(), "running"
    return row


def app_routes(router: APIRouter) -> None:
    @router.get("/apps")
    async def apps(request: Request, admin: AdminUser) -> list[dict[str, Any]]:
        services = services_of(request)
        async with services.db.session() as session:
            rows = list(await session.execute(
                select(HostedApp, Project.name, HostServer.name)
                .join(Project, Project.id == HostedApp.project_id)
                .join(HostServer, HostServer.id == HostedApp.host_id)
                .order_by(HostedApp.app)))  # fmt: skip
            live = list(await session.scalars(select(Deploy).where(Deploy.status == "live")))
            backups = list(await session.scalars(
                select(AppBackup).order_by(AppBackup.created_at.desc())))  # fmt: skip
        return [app_view(hosted, project, host, live, backups) for hosted, project, host in rows]

    @router.post("/apps/{project_id}/backup")
    async def backup_now(
        project_id: str, body: LogsIn, request: Request, admin: AdminUser
    ) -> dict[str, bool]:
        services = services_of(request)
        await hosted_or_404(services, project_id)
        services.background(said(services.backups.backup(project_id, body.environment)))
        await audit(services.db, "hosting.backup_started", user_id=admin.id, target=project_id,
                    ip=client_ip(request), environment=body.environment)  # fmt: skip
        return {"started": True}

    @router.post("/apps/{project_id}/drill")
    async def drill_now(
        project_id: str, body: LogsIn, request: Request, admin: AdminUser
    ) -> dict[str, bool]:
        services = services_of(request)
        await hosted_or_404(services, project_id)
        services.background(said(services.backups.drill(project_id, body.environment)))
        await audit(services.db, "hosting.drill_started", user_id=admin.id, target=project_id,
                    ip=client_ip(request), environment=body.environment)  # fmt: skip
        return {"started": True}

    @router.post("/apps/{project_id}/suspend")
    async def suspend(
        project_id: str, body: Suspension, request: Request, admin: AdminUser
    ) -> dict[str, Any]:
        services = services_of(request)
        sent = await suspend_app(services, project_id, body.reason, suspended=body.suspended)
        action = "hosting.app_suspended" if body.suspended else "hosting.app_resumed"
        await audit(services.db, action, user_id=admin.id, target=project_id, ip=client_ip(request),
                    reason=body.reason[:500], environments=",".join(sent))  # fmt: skip
        return {"suspended": body.suspended, "environments": sent}

    @router.post("/apps/{project_id}/logs")
    async def logs(
        project_id: str, body: LogsIn, request: Request, admin: AdminUser
    ) -> dict[str, Any]:
        services = services_of(request)
        async with services.db.session() as session:
            hosted = await session.get(HostedApp, project_id)
        if hosted is None:
            raise HTTPException(404, "no such app")
        job_id = await send(services, hosted, body.environment, "logs")
        try:
            result = await asyncio.wait_for(services.hosts.result(job_id), LOGS_WAIT_S)
        except TimeoutError:
            raise HTTPException(504, "the host did not answer in time; is it running?") from None
        return {"ok": result.ok, "log_tail": result.log_tail, "error": result.error}


async def suspend_app(
    services: Services, project_id: str, reason: str, *, suspended: bool = True
) -> list[str]:
    """Suspend an app (stop it where it is live, so the edge drops it) or let it run again; the
    environments it was stopped or started in. Also used when a notice is upheld (`abuse.py`)."""
    async with services.db.session() as session, session.begin():
        hosted = await session.get(HostedApp, project_id)
        if hosted is None:
            raise HTTPException(404, "no such app")
        hosted.suspended, hosted.suspended_at = suspended, time.time()
        hosted.suspended_reason = reason.strip() if suspended else ""
    sent = []
    for environment in await live_environments(services, project_id):
        await send(services, hosted, environment, "stop" if suspended else "start")
        sent.append(environment)
    return sent


def app_view(
    hosted: HostedApp, project: str, host: str, live: list[Deploy], backups: list[AppBackup]
) -> dict[str, Any]:
    """A hosted app as the panel shows it: where it runs, what is live, its newest backups."""
    environments = {
        row.environment: {"release": row.release, "commit": row.commit,
                          "url": json.loads(row.data or "{}").get("url", "")}
        for row in live if row.project_id == hosted.project_id
    }  # fmt: skip
    newest: dict[str, dict[str, Any]] = {}
    for row in backups:  # newest first
        if row.project_id == hosted.project_id and row.environment not in newest:
            newest[row.environment] = {"at": row.created_at, "size": row.size,
                                       "drill_at": row.drill_at, "drill_ok": row.drill_ok,
                                       "drill_detail": row.drill_detail}  # fmt: skip
    return {"project_id": hosted.project_id, "app": hosted.app, "project": project, "host": host,
            "suspended": hosted.suspended, "suspended_reason": hosted.suspended_reason,
            "suspended_at": hosted.suspended_at, "live": environments,
            "backups": newest}  # fmt: skip


async def hosted_or_404(services: Services, project_id: str) -> HostedApp:
    """The hosted app of a project."""
    async with services.db.session() as session:
        hosted = await session.get(HostedApp, project_id)
    if hosted is None:
        raise HTTPException(404, "no such app")
    return hosted


async def said(work: Awaitable[str]) -> None:
    """Run a backup or drill on the side; what went wrong goes to the log."""
    problem = await work
    if problem:
        log.warning("hosting: %s", problem)


async def live_environments(services: Services, project_id: str) -> list[str]:
    """The environments in which the project has a live release."""
    async with services.db.session() as session:
        found = await session.scalars(select(Deploy.environment).where(
            Deploy.project_id == project_id, Deploy.status == "live"))  # fmt: skip
        return sorted(set(found))


async def send(services: Services, hosted: HostedApp, environment: str, kind: str) -> str:
    """Queue an admin's job for the app's host; its id."""
    job = HostJob(id=secrets.token_hex(16), kind=kind, app=hosted.app,
                  environment=environment, timeout_s=ADMIN_JOB_TIMEOUT_S)  # fmt: skip
    await services.hosts.send(hosted.host_id, "", job)
    return job.id


def user_routes(router: APIRouter) -> None:
    @router.get("/users")
    async def users(request: Request, admin: AdminUser) -> list[dict[str, Any]]:
        services = services_of(request)
        async with services.db.session() as session:
            rows = await session.scalars(select(User).order_by(User.created_at))
            return [{"id": u.id, "email": u.email, "name": u.name, "role": u.role,
                     "trusted": u.hosting_trusted} for u in rows]  # fmt: skip

    @router.put("/users/{user_id}")
    async def trust(
        user_id: str, body: TrustIn, request: Request, admin: AdminUser
    ) -> dict[str, bool]:
        services = services_of(request)
        async with services.db.session() as session, session.begin():
            row = await session.get(User, user_id)
            if row is None:
                raise HTTPException(404, "no such user")
            row.hosting_trusted = body.trusted
        await audit(services.db, "hosting.trust_changed", user_id=admin.id, target=user_id,
                    ip=client_ip(request), trusted=body.trusted)  # fmt: skip
        return {"trusted": body.trusted}
