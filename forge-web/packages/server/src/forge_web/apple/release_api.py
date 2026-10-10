"""Starting and following releases to TestFlight (W22b): `/api/projects/{id}/apple/releases`.

A release starts only from the project's newest approval (W21), while the project is still at
that commit with nothing uncommitted, for someone who may build Apple apps and has a team key;
it uses that person's key.
"""

import json
import secrets
import time
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from forge_web.apple.approvals import project_state
from forge_web.apple.release import STEPS, view
from forge_web.audit import audit
from forge_web.auth.sessions import CurrentUser, client_ip
from forge_web.db.models import AppleApproval, AppleRelease, AppStoreKey, User
from forge_web.files_api import allowed
from forge_web.services import Services

SHOWN = 20


class ReleaseIn(BaseModel):
    """Which platforms: "ios" (iPhone and iPad, with the Watch app) and "macos"."""

    model_config = ConfigDict(extra="forbid")
    platforms: list[Literal["ios", "macos"]] = Field(min_length=1, max_length=2)


def release_routes() -> APIRouter:
    """List, start and retry the project's releases."""
    router = APIRouter(prefix="/api/projects/{project_id}/apple/releases")

    @router.get("")
    async def listed(project_id: str, request: Request, user: CurrentUser) -> list[dict[str, Any]]:
        services = await allowed(request, user, project_id)
        async with services.db.session() as session:
            rows = await session.scalars(
                select(AppleRelease).where(AppleRelease.project_id == project_id)
                .order_by(AppleRelease.created_at.desc()).limit(SHOWN)
            )  # fmt: skip
            return [view(row) for row in rows]

    @router.post("")
    async def start(
        project_id: str, body: ReleaseIn, request: Request, user: CurrentUser
    ) -> list[dict[str, Any]]:
        services = await allowed(request, user, project_id, "editor")
        approval = await may_release(services, user, project_id)
        rows = []
        for platform in dict.fromkeys(body.platforms):
            rows.append(await new_release(services, user, approval, platform))
        await audit(services.db, "apple.release_started", user_id=user.id, target=project_id,
                    ip=client_ip(request), commit=approval.commit,
                    platforms=",".join(r.platform for r in rows))  # fmt: skip
        for row in rows:
            services.releases.launch(row.id)
        return [view(row) for row in rows]

    @router.post("/{release_id}/retry")
    async def retry(
        project_id: str, release_id: str, request: Request, user: CurrentUser
    ) -> dict[str, Any]:
        services = await allowed(request, user, project_id, "editor")
        async with services.db.session() as session, session.begin():
            row = await session.get(AppleRelease, release_id)
            if row is None or row.project_id != project_id:
                raise HTTPException(404, "no such release")
            if row.user_id != user.id:
                raise HTTPException(403, "only who started the release can start it again")
            if row.status != "failed":
                raise HTTPException(409, "only a failed release can be started again")
            row.status, row.error, row.hint, row.updated_at = "running", "", "", time.time()
        await audit(services.db, "apple.release_retried", user_id=user.id, target=project_id,
                    ip=client_ip(request), release=release_id, step=row.step)  # fmt: skip
        services.releases.launch(release_id)
        return view(row)

    return router


async def may_release(services: Services, user: User, project_id: str) -> AppleApproval:
    """The approval to release, once everything a release needs is there (else 403/409)."""
    if not services.settings.apple.allows(user.role, user.apple_allowed):
        raise HTTPException(403, "you may not build Apple apps on this server")
    limit = services.settings.apple.minutes_per_month
    if limit and await services.apple.minutes_used(user.id) >= limit:
        raise HTTPException(409, "your Mac minutes for this month are used up")
    async with services.db.session() as session:
        if await session.get(AppStoreKey, user.id) is None:
            raise HTTPException(409, "add your App Store Connect key in Settings first")
        approval = await session.scalar(
            select(AppleApproval).where(AppleApproval.project_id == project_id)
            .order_by(AppleApproval.created_at.desc()).limit(1)
        )  # fmt: skip
        running = await session.scalar(
            select(AppleRelease.id).where(AppleRelease.project_id == project_id,
                                          AppleRelease.status == "running").limit(1)
        )  # fmt: skip
    if approval is None or not approval.commit or not approval.clean:
        raise HTTPException(409, "the app has no approval for Apple yet (Ready for approval)")
    if running is not None:
        raise HTTPException(409, "a release of this project is still running")
    commit, clean = await project_state(services, project_id)
    if commit != approval.commit or not clean:
        raise HTTPException(409, "the project changed since it was approved: approve it again")
    return approval


async def new_release(
    services: Services, user: User, approval: AppleApproval, platform: str
) -> AppleRelease:
    """A release row at its first step."""
    number = await services.releases.next_build_number(approval.project_id, platform)
    now = time.time()
    row = AppleRelease(id=secrets.token_hex(8), project_id=approval.project_id, user_id=user.id,
                       approval_id=approval.id, commit=approval.commit, platform=platform,
                       build_number=number, step=STEPS[0], status="running",
                       data=json.dumps({}), created_at=now, updated_at=now)  # fmt: skip
    async with services.db.session() as session, session.begin():
        session.add(row)
    return row
