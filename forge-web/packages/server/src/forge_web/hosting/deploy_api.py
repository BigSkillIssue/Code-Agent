"""The routes of hosting (W26).

- `/api/host/...`: the hosts' API, with their own tokens instead of a sign-in: a host asks for a
  job (long poll), fetches its packed source and, once, its sealed secrets, and reports.
- `/api/projects/{id}/deploys`: a project's deploys. Staging takes a commit the user approved with
  "Ready to go live"; production takes the same commit once it is live in staging, and starting
  it is the creator's "Live schalten". `/api/projects/{id}/hosting/secrets`: the app's secrets
  (values go in, only names come out).
- `/api/projects/{id}/hosting`: everything the go-live page shows (W27).

An admin approves production deploys in `admin.py`.
"""

import json
import secrets
import time
from collections.abc import Awaitable
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import func, or_, select

from forge_hostworker.wire import (
    ENV_NAME,
    RESERVED_ENV,
    HostUsage,
    JobResult,
    PollAnswer,
    PollRequest,
)
from forge_hostworker.wire import SealedSecrets as Sealed
from forge_web.audit import audit
from forge_web.auth.sessions import CurrentUser, client_ip
from forge_web.billing.licenses import require_paid
from forge_web.db.hosting_models import (
    AppBackup,
    AppOperator,
    AppSecret,
    Deploy,
    GoLiveApproval,
    HostedApp,
    HostServer,
)
from forge_web.db.models import Chat, ChatEvent, Project, User
from forge_web.files_api import allowed
from forge_web.hosting.alerts import store_usage
from forge_web.hosting.deploy import ACTIVE, view
from forge_web.hosting.hosts import JobGone, host_of, pick_host
from forge_web.hosting.legal_pages import operator_ready, operator_view
from forge_web.hosting.plan import HOSTING_SETS
from forge_web.services import Services, services_of

POLL_WAIT_S = 25.0
MAX_RESULT_BYTES = 1024 * 1024
SHOWN = 20
REVIEW_STAGES = ("prompt", "plan", "product")
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
        hosts = services_of(request).hosts
        return PollAnswer(job=await hosts.claim(host.id, POLL_WAIT_S, body.running))

    @router.get("/jobs/{job_id}/source")
    async def source(job_id: str, host: Host, request: Request) -> FileResponse:
        path = await refused_unless(services_of(request).hosts.source(host.id, job_id))
        return FileResponse(path, media_type="application/gzip")

    @router.get("/jobs/{job_id}/secrets")
    async def sealed(job_id: str, host: Host, request: Request) -> Sealed:
        return await refused_unless(services_of(request).hosts.secrets_of(host.id, job_id))

    @router.post("/jobs/{job_id}/archive")
    async def archive(job_id: str, host: Host, request: Request) -> dict[str, int]:
        services = services_of(request)
        try:
            target = await services.backups.target_of(job_id)
        except LookupError:
            raise HTTPException(409, "this is not one of this host's archive jobs") from None
        limit = services.settings.hosting.max_backup_mb * 1024 * 1024
        size = await refused_unless(
            services.hosts.store_upload(host.id, job_id, "archive", request.stream(), target, limit)
        )
        return {"size": size}

    @router.post("/metrics")
    async def metrics(body: HostUsage, host: Host, request: Request) -> dict[str, int]:
        return {"kept": await store_usage(services_of(request).db, host, body)}

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

    @router.get("/hosting")
    async def overview(project_id: str, request: Request, user: CurrentUser) -> dict[str, Any]:
        services = await allowed(request, user, project_id)
        return await hosting_overview(services, project_id, user.id)

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
        if body.name in RESERVED_ENV or body.name in HOSTING_SETS:
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


async def new_deploy(services: Services, user: User, project_id: str, body: DeployIn) -> Deploy:
    """A deploy of an approved commit, once everything it needs is there (else 403/409)."""
    if not services.settings.hosting.enabled:
        raise HTTPException(403, "hosting is turned off on this server")
    async with services.db.session() as session:
        project = await session.get(Project, project_id)
        if project is None or project.kind != "app":
            raise HTTPException(409, "only app projects can be hosted")
        commit = await approved_commit(session, project_id, body.commit)
        if body.environment == "production":
            if not await live_in_staging(session, project_id, commit):
                raise HTTPException(409, "deploy this commit to staging first and check it there")
            if not operator_ready(await session.get(AppOperator, project_id)):
                raise HTTPException(409, "enter who operates the app (its Impressum) first")
        busy = await session.scalar(select(Deploy.id).where(
            Deploy.project_id == project_id, Deploy.environment == body.environment,
            Deploy.status.in_(ACTIVE)))  # fmt: skip
        if busy is not None:
            raise HTTPException(409, f"a deploy to {body.environment} is under way")
        hosted = await session.get(HostedApp, project_id)
        if hosted is not None and hosted.suspended:
            raise HTTPException(409, f"an admin suspended this app: {hosted.suspended_reason}")
        if hosted is not None and hosted.deleting_at:
            raise HTTPException(409, "this app is being deleted")
        last = await session.scalar(select(func.max(Deploy.release)).where(
            Deploy.project_id == project_id, Deploy.environment == body.environment))  # fmt: skip
    if hosted is None:
        await room_for_an_app(services, project.owner_id, project_id)
    if body.environment == "production":
        await require_paid(services, project)
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


async def room_for_an_app(services: Services, owner_id: str, project_id: str) -> None:
    """403 when the owner's entitlements allow no further hosted app."""
    limit = (await services.entitlements.of_project(project_id)).hosted_apps
    if limit is None:
        return
    async with services.db.session() as session:
        count = await session.scalar(
            select(func.count()).select_from(HostedApp)
            .join(Project, Project.id == HostedApp.project_id)
            .where(Project.owner_id == owner_id))  # fmt: skip
    if (count or 0) >= limit:
        raise HTTPException(403, f"your plan allows {limit} hosted apps")


async def hosting_overview(services: Services, project_id: str, user_id: str) -> dict[str, Any]:
    """The go-live page's data: reviews, approvals, deploys, what is live, the secrets' names."""
    async with services.db.session() as session:
        hosted = await session.get(HostedApp, project_id)
        deploys = [view(row) for row in await session.scalars(
            select(Deploy).where(Deploy.project_id == project_id)
            .order_by(Deploy.created_at.desc()).limit(SHOWN))]  # fmt: skip
        approvals = [{"commit": a.commit, "at": a.created_at, "summary": a.summary,
                      "chat_id": a.chat_id} for a in await session.scalars(
            select(GoLiveApproval).where(GoLiveApproval.project_id == project_id,
                                         GoLiveApproval.commit != "")
            .order_by(GoLiveApproval.created_at.desc()).limit(SHOWN))]  # fmt: skip
        reviews = await release_reviews(session, project_id, user_id)
        names = [{"environment": s.environment, "name": s.name} for s in await session.scalars(
            select(AppSecret).where(AppSecret.project_id == project_id))]  # fmt: skip
        operator = operator_view(await session.get(AppOperator, project_id))
        backups = [{"id": b.id, "environment": b.environment, "created_at": b.created_at,
                    "size": b.size} for b in await session.scalars(
            select(AppBackup).where(AppBackup.project_id == project_id)
            .order_by(AppBackup.created_at.desc()))]  # fmt: skip
    live = {d["environment"]: d for d in deploys if d["status"] == "live"}
    return {"enabled": services.settings.hosting.enabled, "app": hosted.app if hosted else "",
            "suspended": bool(hosted and hosted.suspended),
            "suspended_reason": hosted.suspended_reason if hosted else "",
            "deleting": bool(hosted and hosted.deleting_at),
            "deleting_error": hosted.deleting_error if hosted else "", "backups": backups,
            "reviews": reviews, "approvals": approvals, "deploys": deploys,
            "staging": live.get("staging"), "production": live.get("production"),
            "secrets": names, "operator": operator}  # fmt: skip


async def release_reviews(session: Any, project_id: str, user_id: str) -> list[dict[str, Any]]:
    """The newest release review of each stage (request, plan, product) from the chats this
    user may read: their own and the shared ones."""
    query = (
        select(ChatEvent.chat_id, ChatEvent.data)
        .join(Chat, Chat.id == ChatEvent.chat_id)
        .where(Chat.project_id == project_id, ChatEvent.kind == "release_review",
               or_(Chat.shared.is_(True), Chat.user_id == user_id))
        .order_by(ChatEvent.ts.desc()).limit(200)
    )  # fmt: skip
    latest: dict[str, dict[str, Any]] = {}
    for chat_id, data in await session.execute(query):
        event = json.loads(data).get("event", {})
        stage = event.get("stage")
        if stage in REVIEW_STAGES and stage not in latest:
            latest[stage] = {**event, "chat_id": chat_id}
    return [latest[stage] for stage in REVIEW_STAGES if stage in latest]
