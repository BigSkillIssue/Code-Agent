"""Project members: owners invite people (who already have an account) as owner, editor or
viewer; they become members when they accept (SEC-W1, `invitations.py`)."""

import logging
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from forge_web.access import require_project
from forge_web.audit import audit
from forge_web.auth.mail import mail_enabled
from forge_web.auth.sessions import CurrentUser, client_ip
from forge_web.chats.one import follow_owner
from forge_web.db.models import Chat, Project, ProjectInvitation, ProjectMember, User
from forge_web.invitations import no_owner_left, offer, pending_invitations, withdraw
from forge_web.services import Services, services_of

log = logging.getLogger(__name__)
MemberRole = Literal["owner", "editor", "viewer"]


class MemberIn(BaseModel):
    """Someone to add."""

    email: str = Field(max_length=320)
    role: MemberRole = "editor"


class RoleIn(BaseModel):
    """A new role."""

    role: MemberRole


async def hand_on(session: AsyncSession, project_id: str, leaving: str) -> None:
    """When the project's owner of record stops being an owner, another owner becomes it: the
    project's plan, limits, payments and notices are theirs from now on. Only members are heirs,
    and nobody is an owner without having accepted it (SEC-W1)."""
    project = await session.get(Project, project_id)
    if project is None or project.owner_id != leaving:
        return
    heir = await session.scalar(select(ProjectMember.user_id).where(
        ProjectMember.project_id == project_id, ProjectMember.role == "owner",
        ProjectMember.user_id != leaving).order_by(ProjectMember.user_id).limit(1))  # fmt: skip
    if heir is not None:
        project.owner_id = heir
        await follow_owner(session, project_id, heir)


async def owners_left(session: AsyncSession, project_id: str, without: str) -> int:
    """How many owners the project keeps if `without` stops being one."""
    count = await session.scalar(
        select(func.count())
        .select_from(ProjectMember)
        .where(
            ProjectMember.project_id == project_id,
            ProjectMember.role == "owner",
            ProjectMember.user_id != without,
        )
    )
    return int(count or 0)


async def stop_chats(services: Services, user_id: str, project_id: str | None = None) -> None:
    """Stop the running turns a user started (in one project, or everywhere)."""
    query = select(Chat).where(Chat.started_by == user_id, Chat.state.in_(("running", "waiting")))
    if project_id is not None:
        query = query.where(Chat.project_id == project_id)
    async with services.db.session() as session:
        chats = list(await session.scalars(query))
    for chat in chats:
        try:
            await services.runs.cancel(chat)
        except Exception as err:  # the sandbox may be gone; the gateway refuses the run anyway
            log.warning("could not stop chat %s: %s", chat.id, err)


async def set_role(session: AsyncSession, member: ProjectMember, role: str) -> None:
    """Give a member a role they already hold or that takes rights away (no consent needed)."""
    project_id, user_id = member.project_id, member.user_id
    if role != "owner" and await owners_left(session, project_id, user_id) == 0:
        raise await no_owner_left(session, project_id)
    member.role = role
    await withdraw(session, project_id, user_id)  # the new role is what counts
    if role != "owner":
        await hand_on(session, project_id, user_id)


def member_view(user: User, role: str, pending: bool = False) -> dict[str, Any]:
    """A member (or someone invited, `pending`) as the web UI sees them."""
    return {"user_id": user.id, "email": user.email, "name": user.name, "role": role,
            "pending": pending}  # fmt: skip


def members_router() -> APIRouter:
    """`/api/projects/{id}/members`."""
    router = APIRouter(prefix="/api/projects/{project_id}/members")

    @router.get("")
    async def list_members(
        project_id: str, request: Request, user: CurrentUser
    ) -> list[dict[str, Any]]:
        async with services_of(request).db.session() as session:
            _, role = await require_project(session, user, project_id)
            rows = await session.execute(
                select(User, ProjectMember.role)
                .join(ProjectMember, ProjectMember.user_id == User.id)
                .where(ProjectMember.project_id == project_id)
                .order_by(User.name)
            )
            members = [member_view(row[0], row[1]) for row in rows]
            # Who was invited is the owners' business until they accept.
            pending = await pending_invitations(session, project_id) if role == "owner" else []
            return members + pending

    @router.post("", status_code=201)
    async def add_member(
        project_id: str, body: MemberIn, request: Request, user: CurrentUser
    ) -> dict[str, Any]:
        services = services_of(request)
        email = body.email.strip().lower()
        if not services.limits.members_by_user.allow(user.id):
            raise HTTPException(429, "too many attempts; try again later")
        async with services.db.session() as session, session.begin():
            await require_project(session, user, project_id, "owner")
            person = await session.scalar(select(User).where(User.email == email))
            if person is None or person.status != "active":
                raise HTTPException(404, "no active account with this email")
            if not person.email_verified and mail_enabled(services.settings.auth.smtp):
                raise HTTPException(409, "this account has not confirmed its email yet")
            if await session.get(ProjectMember, (project_id, person.id)) is not None:
                raise HTTPException(409, "already a member")
            if await session.get(ProjectInvitation, (project_id, person.id)) is not None:
                raise HTTPException(409, "already invited")
            await offer(session, project_id, person.id, body.role, user.id)
        await audit(
            services.db, "member_invited", user_id=user.id, target=project_id,
            ip=client_ip(request), member=person.id, role=body.role,
        )  # fmt: skip
        return member_view(person, body.role, pending=True)

    @router.patch("/{member_id}")
    async def change_role(
        project_id: str, member_id: str, body: RoleIn, request: Request, user: CurrentUser,
        response: Response,
    ) -> dict[str, Any]:  # fmt: skip
        services = services_of(request)
        async with services.db.session() as session, session.begin():
            await require_project(session, user, project_id, "owner")
            member = await session.get(ProjectMember, (project_id, member_id))
            if member is None:
                raise HTTPException(404, "no such member")
            current = member.role
            # An owner may become the owner of record (and pay): only by accepting (SEC-W1).
            promote = body.role == "owner" and current != "owner"
            if promote:
                await offer(session, project_id, member_id, "owner", user.id)
            else:
                await set_role(session, member, body.role)
        if promote:
            await audit(
                services.db, "member_invited", user_id=user.id, target=project_id,
                ip=client_ip(request), member=member_id, role=body.role,
            )  # fmt: skip
            response.status_code = 202
            return {"user_id": member_id, "role": current, "pending": body.role}
        if body.role == "viewer":  # viewers cannot run chats
            await stop_chats(services, member_id, project_id)
        await audit(
            services.db, "member_role", user_id=user.id, target=project_id,
            ip=client_ip(request), member=member_id, role=body.role,
        )  # fmt: skip
        return {"user_id": member_id, "role": body.role}

    @router.delete("/{member_id}", status_code=204)
    async def remove_member(
        project_id: str, member_id: str, request: Request, user: CurrentUser
    ) -> None:
        services = services_of(request)
        # Everyone may leave; only owners may remove others.
        need = "viewer" if member_id == user.id else "owner"
        async with services.db.session() as session, session.begin():
            await require_project(session, user, project_id, need)
            member = await session.get(ProjectMember, (project_id, member_id))
            if member is not None:
                if await owners_left(session, project_id, member_id) == 0:
                    raise await no_owner_left(session, project_id)
                await session.delete(member)
                await hand_on(session, project_id, member_id)
            invited = await withdraw(session, project_id, member_id)
            if member is None and not invited:
                raise HTTPException(404, "no such member")
        if member is not None:
            await stop_chats(services, member_id, project_id)
        await audit(
            services.db, "member_removed" if member is not None else "invitation_withdrawn",
            user_id=user.id, target=project_id, ip=client_ip(request), member=member_id,
        )  # fmt: skip

    return router
