"""The routes of Windows builds (W38a).

- `/api/win/...`: the Windows workers' API, with their own tokens instead of a sign-in: a worker
  asks for a job (long poll), fetches the packed commit, sends the package and reports. It only
  ever sees its own running jobs.
- `/api/projects/{id}/windows/builds`: a project's builds; an editor starts one of the newest
  approved commit, and downloads its package.
- `/api/admin/stores/windows/workers`: the admins add Windows machines (the token is shown once)
  and turn them off.
"""

from collections.abc import Awaitable
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import select

from forge_web.audit import audit
from forge_web.auth.sessions import AdminUser, CurrentUser, client_ip
from forge_web.db.models import Project, User
from forge_web.db.store_models import WindowsBuild, WinWorker
from forge_web.files_api import allowed
from forge_web.hosting.deploy_api import approved_commit
from forge_web.services import Services, services_of
from forge_web.stores.win_workers import create_worker, worker_of, worker_view
from forge_web.stores.windows import ACTIVE, JobRefused, view
from forge_winworker.wire import JobResult, PollAnswer, PollRequest

POLL_WAIT_S = 25.0
MAX_RESULT_BYTES = 1024 * 1024
SHOWN = 20


class NewWorker(BaseModel):
    """A Windows machine to add."""

    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=200)


class WorkerChange(BaseModel):
    """Turn a Windows machine on or off."""

    model_config = ConfigDict(extra="forbid")
    enabled: bool


async def current_worker(request: Request) -> WinWorker:
    """The Windows worker the request's token belongs to; 401 otherwise."""
    services = services_of(request)
    version = request.headers.get("x-forge-win-worker", "")[:40]
    worker = await worker_of(services.db, request.headers.get("authorization", ""), version)
    if worker is None:
        raise HTTPException(401, "not a valid Windows worker token")
    if not services.settings.stores.windows_enabled:
        raise HTTPException(403, "Windows builds are turned off on this server")
    return worker


Worker = Annotated[WinWorker, Depends(current_worker)]


async def refused_unless[T](work: Awaitable[T]) -> T:
    """Run the job operation; a refusal becomes 409."""
    try:
        return await work
    except JobRefused as refusal:
        raise HTTPException(409, str(refusal)) from None


def windows_router() -> APIRouter:
    """The workers' API, the projects' builds and the admins' machines."""
    router = APIRouter()
    worker_routes(router)
    project_routes(router)
    admin_routes(router)
    return router


def worker_routes(router: APIRouter) -> None:
    """`/api/win/...`."""

    @router.post("/api/win/poll")
    async def poll(body: PollRequest, worker: Worker, request: Request) -> PollAnswer:
        return PollAnswer(job=await services_of(request).windows.claim(worker.id, POLL_WAIT_S))

    @router.get("/api/win/jobs/{job_id}/source")
    async def source(job_id: str, worker: Worker, request: Request) -> FileResponse:
        builds = services_of(request).windows
        row = await refused_unless(builds.running(worker.id, job_id))
        return FileResponse(builds.source(row), media_type="application/gzip")

    @router.post("/api/win/jobs/{job_id}/package")
    async def package(job_id: str, worker: Worker, request: Request) -> dict[str, int]:
        services = services_of(request)
        limit = services.settings.stores.max_package_mb * 1024 * 1024
        size = await refused_unless(
            services.windows.store_package(worker.id, job_id, request.stream(), limit))  # fmt: skip
        return {"size": size}

    @router.post("/api/win/jobs/{job_id}/result")
    async def result(job_id: str, worker: Worker, request: Request) -> dict[str, bool]:
        raw = bytearray()
        async for chunk in request.stream():
            raw += chunk
            if len(raw) > MAX_RESULT_BYTES:
                raise HTTPException(413, "the result is too large")
        try:
            report = JobResult.model_validate_json(bytes(raw))
        except ValidationError as err:
            raise HTTPException(422, f"not a valid result: {err.errors()[0]['msg']}") from None
        await refused_unless(services_of(request).windows.finish(worker.id, job_id, report))
        return {"ok": True}


def project_routes(router: APIRouter) -> None:
    """`/api/projects/{id}/windows/builds`."""
    builds_url = "/api/projects/{project_id}/windows/builds"

    @router.get(builds_url)
    async def listed(project_id: str, request: Request, user: CurrentUser) -> list[dict[str, Any]]:
        services = await allowed(request, user, project_id)
        await services.windows.expire()
        async with services.db.session() as session:
            rows = await session.scalars(
                select(WindowsBuild).where(WindowsBuild.project_id == project_id)
                .order_by(WindowsBuild.build_number.desc()).limit(SHOWN)
            )  # fmt: skip
            return [view(row) for row in rows]

    @router.post(builds_url)
    async def start(project_id: str, request: Request, user: CurrentUser) -> dict[str, Any]:
        services = await allowed(request, user, project_id, "editor")
        row = await new_build(services, user, project_id)
        await audit(services.db, "stores.windows_build_started", user_id=user.id,
                    target=project_id, ip=client_ip(request), commit=row.commit,
                    version=row.version)  # fmt: skip
        services.windows.launch(row.id)
        return view(row)

    @router.get(builds_url + "/{build_id}/package")
    async def download(
        project_id: str, build_id: str, request: Request, user: CurrentUser
    ) -> FileResponse:
        services = await allowed(request, user, project_id, "editor")  # who may submit it
        async with services.db.session() as session:
            row = await session.get(WindowsBuild, build_id)
        if row is None or row.project_id != project_id:
            raise HTTPException(404, "no such build")
        path = services.windows.package(row)
        if row.status != "built" or not path.is_file():
            raise HTTPException(409, "this build has no package")
        return FileResponse(path, media_type="application/octet-stream", filename=row.file_name)


async def new_build(services: Services, user: User, project_id: str) -> WindowsBuild:
    """A build of the newest approved commit, once everything it needs is there (else 403/409)."""
    if not services.settings.stores.windows_enabled:
        raise HTTPException(403, "Windows builds are turned off on this server")
    async with services.db.session() as session:
        project = await session.get(Project, project_id)
        if project is None or project.kind != "app":
            raise HTTPException(409, "only app projects have a Windows app")
        commit = await approved_commit(session, project_id, "")
        busy = await session.scalar(select(WindowsBuild.id).where(
            WindowsBuild.project_id == project_id, WindowsBuild.status.in_(ACTIVE)))  # fmt: skip
        if busy is not None:
            raise HTTPException(409, "a Windows build of this app is under way")
    return await refused_unless(services.windows.add(project_id, user.id, commit))


def admin_routes(router: APIRouter) -> None:
    """`/api/admin/stores/windows/workers`."""
    workers_url = "/api/admin/stores/windows/workers"

    @router.get(workers_url)
    async def workers(request: Request, admin: AdminUser) -> list[dict[str, Any]]:
        async with services_of(request).db.session() as session:
            rows = await session.scalars(select(WinWorker).order_by(WinWorker.created_at))
            return [worker_view(row) for row in rows]

    @router.post(workers_url)
    async def add(body: NewWorker, request: Request, admin: AdminUser) -> dict[str, Any]:
        services = services_of(request)
        row, token = await create_worker(services.db, body.name.strip())
        await audit(services.db, "stores.windows_worker_added", user_id=admin.id,
                    target=row.id, ip=client_ip(request), name=row.name)  # fmt: skip
        return worker_view(row) | {"token": token}  # shown this once

    @router.patch(workers_url + "/{worker_id}")
    async def change(
        worker_id: str, body: WorkerChange, request: Request, admin: AdminUser
    ) -> dict[str, Any]:
        services = services_of(request)
        async with services.db.session() as session, session.begin():
            row = await session.get(WinWorker, worker_id)
            if row is None:
                raise HTTPException(404, "no such Windows worker")
            row.enabled = body.enabled
        await audit(services.db, "stores.windows_worker_changed", user_id=admin.id,
                    target=worker_id, ip=client_ip(request), enabled=body.enabled)  # fmt: skip
        return worker_view(row)
