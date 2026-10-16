"""The routes of Google Play releases (W37b).

- `/api/projects/{id}/android/upload-key`: the app's upload certificate, for Play Console.
- `/api/projects/{id}/android/builds/{build_id}/signed`: a bundle signed with the upload key,
  for an app's first upload by hand in Play Console.
- `/api/projects/{id}/android/releases`: a project's releases; an editor starts one of a built,
  approved commit with their own service account.
- `/api/admin/stores/play/...`: the releases waiting for an admin, and the admin's approval.
"""

import secrets
import time
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from forge_web.audit import audit
from forge_web.auth.sessions import AdminUser, CurrentUser, client_ip
from forge_web.db.hosting_models import GoLiveApproval
from forge_web.db.models import User
from forge_web.db.store_models import AndroidBuild, PlayKey, PlayRelease, PlayUploadKey
from forge_web.files_api import allowed
from forge_web.services import Services, services_of
from forge_web.stores.play import Track
from forge_web.stores.play_keys import CLOSED_TEST
from forge_web.stores.play_release import ACTIVE, view
from forge_web.stores.play_signing import fingerprint

SHOWN = 20


class PlayReleaseIn(BaseModel):
    """Which build goes to which track (alpha is the closed test)."""

    model_config = ConfigDict(extra="forbid")
    build_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    track: Track = "internal"
    notes: str = Field(default="", max_length=500)  # what is new, in English
    closed_test_done: bool = False  # a personal account's closed test is behind it


def play_router() -> APIRouter:
    """A project's Play releases and its upload key, and the admins' approval."""
    router = APIRouter()
    project = "/api/projects/{project_id}/android"

    @router.get(project + "/upload-key")
    async def upload_key(project_id: str, request: Request, user: CurrentUser) -> Any:
        services = await allowed(request, user, project_id)
        async with services.db.session() as session:
            row = await session.get(PlayUploadKey, project_id)
        return None if row is None else {"certificate": row.certificate,
                                         "sha256": fingerprint(row.certificate)}  # fmt: skip

    @router.post(project + "/upload-key")
    async def make_upload_key(project_id: str, request: Request, user: CurrentUser) -> Any:
        services = await allowed(request, user, project_id, "editor")
        return await services.play.certificate(project_id)

    @router.get(project + "/builds/{build_id}/signed")
    async def signed(
        project_id: str, build_id: str, request: Request, user: CurrentUser
    ) -> FileResponse:
        services = await allowed(request, user, project_id, "editor")  # who may release it
        build = await built(services, project_id, build_id)
        path = await services.play.signed(build)
        name = f"{project_id}-{build.version_name}-signed.aab"
        return FileResponse(path, media_type="application/octet-stream", filename=name)

    @router.get(project + "/releases")
    async def listed(project_id: str, request: Request, user: CurrentUser) -> list[dict[str, Any]]:
        services = await allowed(request, user, project_id)
        async with services.db.session() as session:
            rows = await session.execute(
                select(PlayRelease, AndroidBuild)
                .join(AndroidBuild, AndroidBuild.id == PlayRelease.build_id)
                .where(PlayRelease.project_id == project_id)
                .order_by(PlayRelease.created_at.desc()).limit(SHOWN)
            )  # fmt: skip
            return [view(release, build) for release, build in rows]

    @router.post(project + "/releases")
    async def start(
        project_id: str, body: PlayReleaseIn, request: Request, user: CurrentUser
    ) -> dict[str, Any]:
        services = await allowed(request, user, project_id, "editor")
        row, build = await new_release(services, user, project_id, body)
        await audit(services.db, "stores.play_release_started", user_id=user.id,
                    target=project_id, ip=client_ip(request), build=build.id,
                    track=row.track, version_code=build.version_code)  # fmt: skip
        if row.status == "releasing":
            services.play.launch(row.id)
        return view(row, build)

    admin_routes(router)
    return router


def admin_routes(router: APIRouter) -> None:
    """The releases waiting for an admin, and approving one."""

    @router.get("/api/admin/stores/play")
    async def waiting(request: Request, admin: AdminUser) -> list[dict[str, Any]]:
        services = services_of(request)
        async with services.db.session() as session:
            rows = await session.execute(
                select(PlayRelease, AndroidBuild)
                .join(AndroidBuild, AndroidBuild.id == PlayRelease.build_id)
                .where(PlayRelease.status == "waiting").order_by(PlayRelease.created_at)
            )  # fmt: skip
            return [view(release, build) | {"project_id": release.project_id}
                    for release, build in rows]  # fmt: skip

    @router.post("/api/admin/stores/play/{release_id}/approve")
    async def approve(release_id: str, request: Request, admin: AdminUser) -> dict[str, Any]:
        services = services_of(request)
        async with services.db.session() as session, session.begin():
            row = await session.get(PlayRelease, release_id)
            if row is None or row.status != "waiting":
                raise HTTPException(409, "this release does not wait for an admin")
            row.status, row.admin, row.admin_id = "releasing", "approved", admin.id
            row.admin_approved_at = row.updated_at = time.time()
        await audit(services.db, "stores.play_release_approved", user_id=admin.id,
                    target=row.project_id, ip=client_ip(request), release=release_id)  # fmt: skip
        services.play.launch(release_id)
        return view(row)


async def built(services: Services, project_id: str, build_id: str) -> AndroidBuild:
    """The project's build with a bundle; 404/409 otherwise."""
    async with services.db.session() as session:
        build = await session.get(AndroidBuild, build_id)
    if build is None or build.project_id != project_id:
        raise HTTPException(404, "no such build")
    if build.status != "built" or not services.android.bundle(build).is_file():
        raise HTTPException(409, "this build has no bundle")
    return build


async def new_release(
    services: Services, user: User, project_id: str, body: PlayReleaseIn
) -> tuple[PlayRelease, AndroidBuild]:
    """A release of an approved build, once everything it needs is there (else 409)."""
    build = await built(services, project_id, body.build_id)
    if not build.package_name:
        raise HTTPException(409, "the Android app names no applicationId in "
                                 "android/app/build.gradle.kts")  # fmt: skip
    async with services.db.session() as session, session.begin():
        approved = await session.scalar(select(GoLiveApproval.id).where(
            GoLiveApproval.project_id == project_id,
            GoLiveApproval.commit == build.commit))  # fmt: skip
        if approved is None:
            raise HTTPException(409, "this build's commit was not approved to go live")
        key = await session.get(PlayKey, user.id)
        if key is None:
            raise HTTPException(409, "add your Google Play service account first (Settings)")
        if key.account == "personal" and body.track == "production" and not body.closed_test_done:
            raise HTTPException(409, CLOSED_TEST)
        busy = await session.scalar(select(PlayRelease.id).where(
            PlayRelease.project_id == project_id, PlayRelease.status.in_(ACTIVE)))  # fmt: skip
        if busy is not None:
            raise HTTPException(409, "a Google Play release of this app is under way")
        trusted = user.role == "admin" or user.hosting_trusted
        now = time.time()
        row = PlayRelease(id=secrets.token_hex(16), project_id=project_id,
                          build_id=build.id, user_id=user.id, track=body.track, notes=body.notes,
                          status="releasing" if trusted else "waiting",
                          admin=("admin" if user.role == "admin" else "trusted") if trusted else "",
                          created_at=now, updated_at=now)  # fmt: skip
        session.add(row)
    return row, build
