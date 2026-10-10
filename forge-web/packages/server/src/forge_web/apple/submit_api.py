"""The App Store step of the page "Release" (W22d2): `/api/projects/{id}/apple/submissions`.

Three clicks, each the user's own: "Prepare for the App Store" (fills the version in App Store
Connect), "Submit to Apple" (only with a confirmation naming the version, and only while the
release still belongs to the project's newest approval), and "Release" once Apple approved.
Each is in the audit log.
"""

import json
import secrets
import time
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Request
from forge.apple_listing import StoreListing
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from forge_web.apple.listing import missing
from forge_web.apple.submission import STEPS, view
from forge_web.audit import audit
from forge_web.auth.sessions import CurrentUser, client_ip
from forge_web.db.models import AppleApproval, AppleListing, AppleRelease, AppleSubmission, User
from forge_web.files_api import allowed
from forge_web.services import Services

SHOWN = 20
EMAIL = r"^[^@\s]{1,64}@[^@\s]{1,255}\.[^@\s]{2,63}$"


class ContactIn(BaseModel):
    """Who App Review may contact about the app."""

    model_config = ConfigDict(extra="forbid")
    first_name: str = Field(min_length=1, max_length=100)
    last_name: str = Field(min_length=1, max_length=100)
    phone: str = Field(pattern=r"^\+?[0-9 ()./-]{6,30}$")
    email: str = Field(pattern=EMAIL, max_length=320)
    notes: str = Field(default="", max_length=4000)  # for App Review


class PrepareIn(BaseModel):
    """Which release goes to the App Store, and the contact for App Review."""

    model_config = ConfigDict(extra="forbid")
    release_id: str = Field(pattern=r"^[0-9a-f]{16}$")
    contact: ContactIn


class ConfirmIn(BaseModel):
    """The user's confirmation, naming the version they saw."""

    model_config = ConfigDict(extra="forbid")
    confirm: Literal[True]
    version: str = Field(min_length=1, max_length=32)


def submission_routes() -> APIRouter:
    """List, prepare, submit and release."""
    router = APIRouter(prefix="/api/projects/{project_id}/apple/submissions")

    @router.get("")
    async def listed(project_id: str, request: Request, user: CurrentUser) -> list[dict[str, Any]]:
        services = await allowed(request, user, project_id)
        async with services.db.session() as session:
            rows = await session.scalars(
                select(AppleSubmission).where(AppleSubmission.project_id == project_id)
                .order_by(AppleSubmission.created_at.desc()).limit(SHOWN)
            )  # fmt: skip
            return [view(row) for row in rows]

    @router.post("")
    async def prepare(
        project_id: str, body: PrepareIn, request: Request, user: CurrentUser
    ) -> dict[str, Any]:
        services = await allowed(request, user, project_id, "editor")
        release = await current_release(services, user, project_id, body.release_id)
        await listing_ready(services, project_id)
        row = await new_submission(services, user, release, body.contact)
        await audit(services.db, "apple.submission_prepared", user_id=user.id, target=project_id,
                    ip=client_ip(request), release=release.id, version=row.version)  # fmt: skip
        services.submissions.launch(row.id)
        return view(row)

    @router.post("/{submission_id}/submit")
    async def send(
        project_id: str, submission_id: str, body: ConfirmIn, request: Request, user: CurrentUser
    ) -> dict[str, Any]:
        services = await allowed(request, user, project_id, "editor")
        row = await own_submission(services, user, project_id, submission_id)
        if row.status != "ready":
            raise HTTPException(409, "only a prepared submission can be sent to Apple")
        if body.version != row.version:
            raise HTTPException(409, f"the confirmation names {body.version}, not {row.version}")
        await current_release(services, user, project_id, row.release_id)
        await services.submissions.send(row.id)
        await audit(services.db, "apple.submitted", user_id=user.id, target=project_id,
                    ip=client_ip(request), version=row.version, platform=row.platform)  # fmt: skip
        return view(await services.submissions.row(row.id))

    @router.post("/{submission_id}/release")
    async def publish(
        project_id: str, submission_id: str, request: Request, user: CurrentUser
    ) -> dict[str, Any]:
        services = await allowed(request, user, project_id, "editor")
        row = await own_submission(services, user, project_id, submission_id)
        state = json.loads(row.data or "{}").get("version_state", "")
        if row.status != "submitted" or state != "PENDING_DEVELOPER_RELEASE":
            raise HTTPException(409, "Apple has not approved this version for release yet")
        await services.submissions.publish(row.id)
        await audit(services.db, "apple.released", user_id=user.id, target=project_id,
                    ip=client_ip(request), version=row.version, platform=row.platform)  # fmt: skip
        return view(await services.submissions.row(row.id))

    return router


async def current_release(
    services: Services, user: User, project_id: str, release_id: str
) -> AppleRelease:
    """The user's release in TestFlight, as long as it belongs to the newest approval."""
    if not services.settings.apple.allows(user.role, user.apple_allowed):
        raise HTTPException(403, "you may not build Apple apps on this server")
    async with services.db.session() as session:
        release = await session.get(AppleRelease, release_id)
        newest = await session.scalar(
            select(AppleApproval.id).where(AppleApproval.project_id == project_id)
            .order_by(AppleApproval.created_at.desc()).limit(1)
        )  # fmt: skip
    if release is None or release.project_id != project_id:
        raise HTTPException(404, "no such release")
    if release.user_id != user.id:
        raise HTTPException(403, "only who released it can send it to Apple (with their key)")
    if release.status != "done":
        raise HTTPException(409, "the release is not in TestFlight yet")
    if release.approval_id != newest:
        raise HTTPException(409, "the app was approved again since: release that version first")
    return release


async def listing_ready(services: Services, project_id: str) -> StoreListing:
    """The saved store texts, with everything Apple needs."""
    async with services.db.session() as session:
        saved = await session.get(AppleListing, project_id)
    if saved is None:
        raise HTTPException(409, "save the store texts first (Store texts)")
    listing = StoreListing.model_validate_json(saved.data)
    if missing(listing):
        raise HTTPException(409, "the store texts still need: " + ", ".join(missing(listing)))
    return listing


async def new_submission(
    services: Services, user: User, release: AppleRelease, contact: ContactIn
) -> AppleSubmission:
    """A submission at its first step (another for the same release replaces nothing)."""
    async with services.db.session() as session:
        busy = await session.scalar(
            select(AppleSubmission.id).where(
                AppleSubmission.project_id == release.project_id,
                AppleSubmission.platform == release.platform,
                AppleSubmission.status.in_(("preparing", "submitted")),
            ).limit(1)
        )  # fmt: skip
    if busy is not None:
        raise HTTPException(409, "this platform's last submission is still under way")
    version = str(json.loads(release.data or "{}").get("version", ""))
    now = time.time()
    row = AppleSubmission(
        id=secrets.token_hex(8),
        project_id=release.project_id,
        user_id=user.id,
        release_id=release.id,
        platform=release.platform,
        version=version,
        status="preparing",
        step=STEPS[0],
        contact=contact.model_dump_json(),
        created_at=now,
        updated_at=now,
    )
    async with services.db.session() as session, session.begin():
        session.add(row)
    return row  # fmt: skip


async def own_submission(
    services: Services, user: User, project_id: str, submission_id: str
) -> AppleSubmission:
    """A submission of this project that the user made."""
    async with services.db.session() as session:
        row = await session.get(AppleSubmission, submission_id)
    if row is None or row.project_id != project_id:
        raise HTTPException(404, "no such submission")
    if row.user_id != user.id:
        raise HTTPException(403, "only who prepared it can send or release it")
    return row
