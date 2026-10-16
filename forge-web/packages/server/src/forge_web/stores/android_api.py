"""The routes of Android builds (W37a).

- `/api/projects/{id}/android/builds`: a project's builds; an editor starts one of the newest
  approved commit, once the app is on a host (its first staging deploy put it there).
- `/api/projects/{id}/android/builds/{build_id}/bundle`: the unsigned release bundle, for the
  editors (who may release it).
- `/api/host/jobs/{job_id}/artifact`: where the host's android job sends that bundle.
"""

from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse
from sqlalchemy import select

from forge_web.audit import audit
from forge_web.auth.sessions import CurrentUser, client_ip
from forge_web.db.hosting_models import HostedApp
from forge_web.db.models import Project, User
from forge_web.db.store_models import AndroidBuild
from forge_web.files_api import allowed
from forge_web.hosting.deploy_api import Host, approved_commit, refused_unless
from forge_web.services import Services, services_of
from forge_web.stores.android import BuildProblem, view

SHOWN = 20


def android_router() -> APIRouter:
    """A project's Android builds, and the hosts' upload of their bundles."""
    router = APIRouter()

    @router.post("/api/host/jobs/{job_id}/artifact")
    async def artifact(job_id: str, host: Host, request: Request) -> dict[str, int]:
        services = services_of(request)
        try:
            target = await services.android.target_of(job_id)
        except LookupError:
            raise HTTPException(409, "this is not one of this host's android jobs") from None
        limit = services.settings.hosting.max_bundle_mb * 1024 * 1024
        size = await refused_unless(services.hosts.store_upload(
            host.id, job_id, "android", request.stream(), target, limit))  # fmt: skip
        return {"size": size}

    @router.get("/api/projects/{project_id}/android/builds")
    async def listed(project_id: str, request: Request, user: CurrentUser) -> list[dict[str, Any]]:
        services = await allowed(request, user, project_id)
        async with services.db.session() as session:
            rows = await session.scalars(
                select(AndroidBuild).where(AndroidBuild.project_id == project_id)
                .order_by(AndroidBuild.version_code.desc()).limit(SHOWN)
            )  # fmt: skip
            return [view(row) for row in rows]

    @router.post("/api/projects/{project_id}/android/builds")
    async def start(project_id: str, request: Request, user: CurrentUser) -> dict[str, Any]:
        services = await allowed(request, user, project_id, "editor")
        row = await new_build(services, user, project_id)
        await audit(services.db, "stores.android_build_started", user_id=user.id,
                    target=project_id, ip=client_ip(request), commit=row.commit,
                    version_code=row.version_code)  # fmt: skip
        services.android.launch(row.id)
        return view(row)

    @router.get("/api/projects/{project_id}/android/builds/{build_id}/bundle")
    async def bundle(
        project_id: str, build_id: str, request: Request, user: CurrentUser
    ) -> FileResponse:
        services = await allowed(request, user, project_id, "editor")  # who may release it
        async with services.db.session() as session:
            row = await session.get(AndroidBuild, build_id)
        if row is None or row.project_id != project_id:
            raise HTTPException(404, "no such build")
        path = services.android.bundle(row)
        if row.status != "built" or not path.is_file():
            raise HTTPException(409, "this build has no bundle")
        name = f"{project_id}-{row.version_name}.aab"
        return FileResponse(path, media_type="application/octet-stream", filename=name)

    return router


async def new_build(services: Services, user: User, project_id: str) -> AndroidBuild:
    """A build of the newest approved commit, once everything it needs is there (else 403/409)."""
    hosting = services.settings.hosting
    if not hosting.enabled:
        raise HTTPException(403, "hosting is turned off on this server")
    if not hosting.android_image:
        raise HTTPException(409, "this server builds no Android apps: an admin sets "
                            "hosting.android_image first")  # fmt: skip
    async with services.db.session() as session:
        project = await session.get(Project, project_id)
        if project is None or project.kind != "app":
            raise HTTPException(409, "only app projects have an Android app")
        commit = await approved_commit(session, project_id, "")
        hosted = await session.get(HostedApp, project_id)
        if hosted is None:
            raise HTTPException(409, "deploy the app to staging first: its host builds it")
        if hosted.suspended:
            raise HTTPException(409, f"an admin suspended this app: {hosted.suspended_reason}")
        if hosted.deleting_at:
            raise HTTPException(409, "this app is being deleted")
        busy = await session.scalar(select(AndroidBuild.id).where(
            AndroidBuild.project_id == project_id, AndroidBuild.status == "building"))  # fmt: skip
        if busy is not None:
            raise HTTPException(409, "an Android build of this app is under way")
    try:
        return await services.android.add(project_id, user.id, commit)
    except BuildProblem as problem:
        raise HTTPException(409, str(problem)) from None
