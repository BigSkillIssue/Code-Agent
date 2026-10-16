"""The routes of Microsoft Store submissions (W38b).

- `/api/projects/{id}/windows/submissions`: a project's submissions; an editor submits a built
  Windows build of an approved commit to the app's Store listing with their own Entra ID app.
- `/api/admin/stores/msstore/...`: the submissions waiting for an admin, and the approval.
"""

import secrets
import time
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from forge_web.audit import audit
from forge_web.auth.sessions import AdminUser, CurrentUser, client_ip
from forge_web.db.hosting_models import GoLiveApproval
from forge_web.db.models import User
from forge_web.db.store_models import MsStoreKey, MsSubmission, WindowsBuild
from forge_web.files_api import allowed
from forge_web.services import Services, services_of
from forge_web.stores.msstore import STORE_ID
from forge_web.stores.msstore_release import ACTIVE, view

SHOWN = 20


class SubmissionIn(BaseModel):
    """Which build goes to which Store listing."""

    model_config = ConfigDict(extra="forbid")
    build_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    store_id: str = Field(pattern=STORE_ID)  # the app's Store ID in Partner Center


def msstore_router() -> APIRouter:
    """A project's submissions, and the admins' approval."""
    router = APIRouter()
    url = "/api/projects/{project_id}/windows/submissions"

    @router.get(url)
    async def listed(project_id: str, request: Request, user: CurrentUser) -> list[dict[str, Any]]:
        services = await allowed(request, user, project_id)
        async with services.db.session() as session:
            ids = list(await session.scalars(select(MsSubmission.id).where(
                MsSubmission.project_id == project_id,
                MsSubmission.status == "submitted")))  # fmt: skip
        for submission_id in ids:
            await services.msstore.follow(submission_id)
        async with services.db.session() as session:
            rows = await session.execute(
                select(MsSubmission, WindowsBuild)
                .join(WindowsBuild, WindowsBuild.id == MsSubmission.build_id)
                .where(MsSubmission.project_id == project_id)
                .order_by(MsSubmission.created_at.desc()).limit(SHOWN)
            )  # fmt: skip
            return [view(row, build) for row, build in rows]

    @router.post(url)
    async def start(
        project_id: str, body: SubmissionIn, request: Request, user: CurrentUser
    ) -> dict[str, Any]:
        services = await allowed(request, user, project_id, "editor")
        row, build = await new_submission(services, user, project_id, body)
        await audit(services.db, "stores.msstore_submission_started", user_id=user.id,
                    target=project_id, ip=client_ip(request), build=build.id,
                    store_id=row.store_id, version=build.version)  # fmt: skip
        if row.status == "submitting":
            services.msstore.launch(row.id)
        return view(row, build)

    admin_routes(router)
    return router


def admin_routes(router: APIRouter) -> None:
    """The submissions waiting for an admin, and approving one."""

    @router.get("/api/admin/stores/msstore")
    async def waiting(request: Request, admin: AdminUser) -> list[dict[str, Any]]:
        async with services_of(request).db.session() as session:
            rows = await session.execute(
                select(MsSubmission, WindowsBuild)
                .join(WindowsBuild, WindowsBuild.id == MsSubmission.build_id)
                .where(MsSubmission.status == "waiting").order_by(MsSubmission.created_at)
            )  # fmt: skip
            return [view(row, build) | {"project_id": row.project_id} for row, build in rows]

    @router.post("/api/admin/stores/msstore/{submission_id}/approve")
    async def approve(submission_id: str, request: Request, admin: AdminUser) -> dict[str, Any]:
        services = services_of(request)
        async with services.db.session() as session, session.begin():
            row = await session.get(MsSubmission, submission_id)
            if row is None or row.status != "waiting":
                raise HTTPException(409, "this submission does not wait for an admin")
            row.status, row.admin, row.admin_id = "submitting", "approved", admin.id
            row.admin_approved_at = row.updated_at = time.time()
        await audit(services.db, "stores.msstore_submission_approved", user_id=admin.id,
                    target=row.project_id, ip=client_ip(request),
                    submission=submission_id)  # fmt: skip
        services.msstore.launch(submission_id)
        return view(row)


async def new_submission(
    services: Services, user: User, project_id: str, body: SubmissionIn
) -> tuple[MsSubmission, WindowsBuild]:
    """A submission of an approved, built package, once everything it needs is there."""
    async with services.db.session() as session, session.begin():
        build = await session.get(WindowsBuild, body.build_id)
        if build is None or build.project_id != project_id:
            raise HTTPException(404, "no such build")
        if build.status != "built" or not services.windows.package(build).is_file():
            raise HTTPException(409, "this build has no package")
        approved = await session.scalar(select(GoLiveApproval.id).where(
            GoLiveApproval.project_id == project_id,
            GoLiveApproval.commit == build.commit))  # fmt: skip
        if approved is None:
            raise HTTPException(409, "this build's commit was not approved to go live")
        if await session.get(MsStoreKey, user.id) is None:
            raise HTTPException(409, "add your Entra ID app for the Microsoft Store first")
        busy = await session.scalar(select(MsSubmission.id).where(
            MsSubmission.project_id == project_id, MsSubmission.status.in_(ACTIVE)))  # fmt: skip
        if busy is not None:
            raise HTTPException(409, "a Microsoft Store submission of this app is under way")
        trusted = user.role == "admin" or user.hosting_trusted
        given = ("admin" if user.role == "admin" else "trusted") if trusted else ""
        now = time.time()
        row = MsSubmission(id=secrets.token_hex(16), project_id=project_id, build_id=build.id,
                           user_id=user.id, store_id=body.store_id,
                           status="submitting" if trusted else "waiting",
                           admin=given,
                           created_at=now, updated_at=now)  # fmt: skip
        session.add(row)
    return row, build
