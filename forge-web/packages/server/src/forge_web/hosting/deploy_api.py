"""The routes of hosting (W26).

- `/api/host/...`: the hosts' API, with their own tokens instead of a sign-in: a host asks for a
  job (long poll), fetches its packed source and, once, its sealed secrets, and reports.
- `/api/projects/{id}/deploys`: a project's deploys. Staging takes a commit the user approved with
  "Ready to go live"; production takes the same commit once it is live in staging, and starting
  it is the creator's "Live schalten". `/api/projects/{id}/hosting/secrets`: the app's secrets
  (values go in, only names come out).
- `/api/admin/hosting/deploys/{id}/approve`: an admin's approval of a production deploy.
"""

import json
import secrets
import time
from collections.abc import Awaitable
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import func, select

from forge_hostworker.wire import ENV_NAME, RESERVED_ENV, JobResult, PollAnswer, PollRequest
from forge_hostworker.wire import SealedSecrets as Sealed
from forge_web.audit import audit
from forge_web.auth.sessions import AdminUser, CurrentUser, client_ip
from forge_web.db.hosting_models import AppSecret, Deploy, GoLiveApproval, HostedApp, HostServer
from forge_web.db.models import Project, User
from forge_web.files_api import allowed
from forge_web.hosting.deploy import ACTIVE, view
from forge_web.hosting.hosts import JobGone, host_of, pick_host
from forge_web.services import Services, services_of

POLL_WAIT_S = 25.0
MAX_RESULT_BYTES = 1024 * 1024
SHOWN = 20
Environment = Literal["staging", "production"]


class DeployIn(BaseModel):
    """Which environment, and which approved commit (empty: the newest approval's)."""

    model_config = ConfigDict(extra="forbid")
    environment: Environment
    commit: str = Field(default="", pattern=r"^$|^[0-9a-f]{40,64}$")


class SecretIn(BaseModel):
    """One secret value of the app in one environment."""

    model_config = ConfigDict(extra="forbid")
    environment: Environment
    name: str = Field(pattern=ENV_NAME)
    value: str = Field(min_length=1, max_length=8_000)


async def current_host(request: Request) -> HostServer:
    """The host the request's token belongs to; 401 otherwise."""
    services = services_of(request)
    version = request.headers.get("x-forge-host-worker", "")[:40]
    host = await host_of(services.db, request.headers.get("authorization", ""), version)
    if host is None:
        raise HTTPException(401, "not a valid host token")
    if not services.settings.hosting.enabled:
        raise HTTPException(403, "hosting is turned off on this server")
    return host


Host = Annotated[HostServer, Depends(current_host)]


def host_router() -> APIRouter:
    """Routes for hosts."""
    router = APIRouter(prefix="/api/host")

    @router.post("/poll")
    async def poll(body: PollRequest, host: Host, request: Request) -> PollAnswer:
        return PollAnswer(job=await services_of(request).hosts.claim(host.id, POLL_WAIT_S))

    @router.get("/jobs/{job_id}/source")
    async def source(job_id: str, host: Host, request: Request) -> FileResponse:
        path = await refused_unless(services_of(request).hosts.source(host.id, job_id))
        return FileResponse(path, media_type="application/gzip")

    @router.get("/jobs/{job_id}/secrets")
    async def sealed(job_id: str, host: Host, request: Request) -> Sealed:
        return await refused_unless(services_of(request).hosts.secrets_of(host.id, job_id))

    @router.post("/jobs/{job_id}/result")
    async def result(job_id: str, host: Host, request: Request) -> dict[str, bool]:
        raw = bytearray()
        async for chunk in request.stream():
            raw += chunk
            if len(raw) > MAX_RESULT_BYTES:
                raise HTTPException(413, "the result is too large")
        try:
            report = JobResult.model_validate_json(bytes(raw))
        except ValidationError as err:
            raise HTTPException(422, f"not a valid result: {err.errors()[0]['msg']}") from None
        await refused_unless(services_of(request).hosts.finish(host.id, job_id, report))
        return {"ok": True}

    return router


async def refused_unless[T](work: Awaitable[T]) -> T:
    """Run the job operation; a refusal becomes 409."""
    try:
        return await work
    except JobGone as refusal:
        raise HTTPException(409, str(refusal)) from None


def deploy_router() -> APIRouter:
    """A project's deploys and its app's secrets."""
    router = APIRouter(prefix="/api/projects/{project_id}")

    @router.get("/deploys")
    async def listed(project_id: str, request: Request, user: CurrentUser) -> list[dict[str, Any]]:
        services = await allowed(request, user, project_id)
        async with services.db.session() as session:
            rows = await session.scalars(
                select(Deploy).where(Deploy.project_id == project_id)
                .order_by(Deploy.created_at.desc()).limit(SHOWN)
            )  # fmt: skip
            return [view(row) for row in rows]

    @router.post("/deploys")
    async def start(
        project_id: str, body: DeployIn, request: Request, user: CurrentUser
    ) -> dict[str, Any]:
        services = await allowed(request, user, project_id, "editor")
        row = await new_deploy(services, user, project_id, body)
        await audit(services.db, "hosting.deploy_started", user_id=user.id, target=project_id,
                    ip=client_ip(request), environment=row.environment, commit=row.commit,
                    release=row.release)  # fmt: skip
        services.deploys.launch(row.id)
        return view(row)

    @router.post("/deploys/{deploy_id}/retry")
    async def retry(
        project_id: str, deploy_id: str, request: Request, user: CurrentUser
    ) -> dict[str, Any]:
        services = await allowed(request, user, project_id, "editor")
        row = await restarted(services, user, project_id, deploy_id)
        await audit(services.db, "hosting.deploy_retried", user_id=user.id, target=project_id,
                    ip=client_ip(request), deploy=deploy_id, step=row.step)  # fmt: skip
        services.deploys.launch(deploy_id)
        return view(row)

    secret_routes(router)
    return router


def secret_routes(router: APIRouter) -> None:
    """`/hosting/secrets`: names come out, values only go in."""

    @router.get("/hosting/secrets")
    async def names(project_id: str, request: Request, user: CurrentUser) -> list[dict[str, Any]]:
        services = await allowed(request, user, project_id)
        async with services.db.session() as session:
            rows = await session.scalars(select(AppSecret).where(
                AppSecret.project_id == project_id).order_by(AppSecret.name))  # fmt: skip
            return [{"environment": r.environment, "name": r.name, "updated_at": r.updated_at}
                    for r in rows]  # fmt: skip

    @router.put("/hosting/secrets")
    async def set_secret(
        project_id: str, body: SecretIn, request: Request, user: CurrentUser
    ) -> dict[str, bool]:
        services = await allowed(request, user, project_id, "editor")
        if body.name in RESERVED_ENV:
            raise HTTPException(422, f"{body.name} is set by hosting itself")
        key = (project_id, body.environment, body.name)
        async with services.db.session() as session, session.begin():
            row = await session.get(AppSecret, key)
            if row is None:
                row = AppSecret(project_id=project_id, environment=body.environment,
                                name=body.name, value="", updated_at=0.0)  # fmt: skip
                session.add(row)
            row.value, row.updated_at = services.vault.encrypt(body.value), time.time()
        await audit(services.db, "hosting.secret_set", user_id=user.id, target=project_id,
                    ip=client_ip(request), environment=body.environment,
                    name=body.name)  # fmt: skip
        return {"ok": True}


def admin_deploy_router() -> APIRouter:
    """An admin's approval of a production deploy."""
    router = APIRouter(prefix="/api/admin/hosting")

    @router.post("/deploys/{deploy_id}/approve")
    async def approve(deploy_id: str, request: Request, admin: AdminUser) -> dict[str, Any]:
        services = services_of(request)
        async with services.db.session() as session, session.begin():
            row = await session.get(Deploy, deploy_id)
            if row is None:
                raise HTTPException(404, "no such deploy")
            if (row.status, row.step) != ("waiting", "admin"):
                raise HTTPException(409, "this deploy does not wait for an admin")
            row.admin_id, row.admin_approved_at = admin.id, time.time()
            row.status, row.updated_at = "running", time.time()
        await audit(services.db, "hosting.deploy_approved", user_id=admin.id, target=row.project_id,
                    ip=client_ip(request), deploy=deploy_id, commit=row.commit)  # fmt: skip
        services.deploys.launch(deploy_id)
        return view(row)

    return router


async def new_deploy(services: Services, user: User, project_id: str, body: DeployIn) -> Deploy:
    """A deploy of an approved commit, once everything it needs is there (else 403/409)."""
    if not services.settings.hosting.enabled:
        raise HTTPException(403, "hosting is turned off on this server")
    async with services.db.session() as session:
        project = await session.get(Project, project_id)
        if project is None or project.kind != "app":
            raise HTTPException(409, "only app projects can be hosted")
        commit = await approved_commit(session, project_id, body.commit)
        if body.environment == "production" and not await live_in_staging(session, project_id,
                                                                          commit):  # fmt: skip
            raise HTTPException(409, "deploy this commit to staging first and check it there")
        busy = await session.scalar(select(Deploy.id).where(
            Deploy.project_id == project_id, Deploy.environment == body.environment,
            Deploy.status.in_(ACTIVE)))  # fmt: skip
        if busy is not None:
            raise HTTPException(409, f"a deploy to {body.environment} is under way")
        hosted = await session.get(HostedApp, project_id)
        last = await session.scalar(select(func.max(Deploy.release)).where(
            Deploy.project_id == project_id, Deploy.environment == body.environment))  # fmt: skip
    picked = None if hosted else await pick_host(services.db)
    host = hosted.host_id if hosted else picked.id if picked else None
    if host is None:
        raise HTTPException(409, "there is no host for apps yet; an admin adds one")
    now = time.time()
    row = Deploy(id=secrets.token_hex(8), project_id=project_id, user_id=user.id, app="",
                 host_id=host, environment=body.environment, commit=commit,
                 release=int(last or 0) + 1, step="pack", status="running",
                 creator_approved_at=now if body.environment == "production" else 0.0,
                 data="{}", created_at=now, updated_at=now)  # fmt: skip
    async with services.db.session() as session, session.begin():
        session.add(row)
    return row


async def approved_commit(session: Any, project_id: str, commit: str) -> str:
    """The commit to deploy: one the user said is "Ready to go live" (409 for any other)."""
    query = select(GoLiveApproval).where(GoLiveApproval.project_id == project_id,
                                         GoLiveApproval.commit != "")  # fmt: skip
    if commit:
        query = query.where(GoLiveApproval.commit == commit)
    approval = await session.scalar(query.order_by(GoLiveApproval.created_at.desc()).limit(1))
    if approval is None:
        raise HTTPException(409, "this commit was not approved to go live"
                            if commit else "no commit was approved to go live yet")  # fmt: skip
    return str(approval.commit)


async def live_in_staging(session: Any, project_id: str, commit: str) -> bool:
    """Whether this commit is live and healthy in staging now."""
    found = await session.scalar(select(Deploy.id).where(
        Deploy.project_id == project_id, Deploy.environment == "staging",
        Deploy.commit == commit, Deploy.status == "live"))  # fmt: skip
    return found is not None


async def restarted(services: Services, user: User, project_id: str, deploy_id: str) -> Deploy:
    """A failed deploy set to start again at the step that failed (that step's job anew)."""
    async with services.db.session() as session, session.begin():
        row = await session.get(Deploy, deploy_id)
        if row is None or row.project_id != project_id:
            raise HTTPException(404, "no such deploy")
        if row.user_id != user.id:
            raise HTTPException(403, "only who started the deploy can start it again")
        if row.status != "failed":
            raise HTTPException(409, "only a failed deploy can be started again")
        data = json.loads(row.data or "{}")
        data.pop(f"{row.step}_job", None)
        if row.step == "check":
            data.pop("checks", None)
            data.pop("checks_from", None)
        row.data, row.status, row.error, row.hint = json.dumps(data), "running", "", ""
        row.updated_at = time.time()
    return row
