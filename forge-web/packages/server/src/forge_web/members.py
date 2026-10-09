"""Project members: owners add people (who already have an account) as owner, editor or viewer."""

import logging
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from forge_web.access import require_project
from forge_web.audit import audit
from forge_web.auth.mail import mail_enabled
from forge_web.auth.sessions import CurrentUser, client_ip
from forge_web.db.models import Chat, ProjectMember, User
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
    """Stop the running turns of a user's chats (in one project, or everywhere)."""
    query = select(Chat).where(Chat.user_id == user_id, Chat.state.in_(("running", "waiting")))
    if project_id is not None:
        query = query.where(Chat.project_id == project_id)
    async with services.db.session() as session:
        chats = list(await session.scalars(query))
    for chat in chats:
        try:
            await services.runs.cancel(chat)
        except Exception as err:  # the sandbox may be gone; the gateway refuses the run anyway
            log.warning("could not stop chat %s: %s", chat.id, err)


def member_view(user: User, role: str) -> dict[str, Any]:
    """A member as the web UI sees them."""
    return {"user_id": user.id, "email": user.email, "name": user.name, "role": role}


def members_router() -> APIRouter:
    """`/api/projects/{id}/members`."""
    router = APIRouter(prefix="/api/projects/{project_id}/members")

    @router.get("")
    async def list_members(
        project_id: str, request: Request, user: CurrentUser
    ) -> list[dict[str, Any]]:
        async with services_of(request).db.session() as session:
            await require_project(session, user, project_id)
            rows = await session.execute(
                select(User, ProjectMember.role)
                .join(ProjectMember, ProjectMember.user_id == User.id)
                .where(ProjectMember.project_id == project_id)
                .order_by(User.name)
            )
            return [member_view(row[0], row[1]) for row in rows]

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
            session.add(ProjectMember(project_id=project_id, user_id=person.id, role=body.role))
        await audit(
            services.db, "member_added", user_id=user.id, target=project_id,
            ip=client_ip(request), member=person.id, role=body.role,
        )  # fmt: skip
        return member_view(person, body.role)

    @router.patch("/{member_id}")
    async def change_role(
        project_id: str, member_id: str, body: RoleIn, request: Request, user: CurrentUser
    ) -> dict[str, Any]:
        services = services_of(request)
        async with services.db.session() as session, session.begin():
            await require_project(session, user, project_id, "owner")
            member = await session.get(ProjectMember, (project_id, member_id))
            if member is None:
                raise HTTPException(404, "no such member")
            if body.role != "owner" and await owners_left(session, project_id, member_id) == 0:
                raise HTTPException(409, "a project needs at least one owner")
            member.role = body.role
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
            if member is None:
                raise HTTPException(404, "no such member")
            if await owners_left(session, project_id, member_id) == 0:
                raise HTTPException(409, "a project needs at least one owner")
            await session.delete(member)
        await stop_chats(services, member_id, project_id)
        await audit(
            services.db, "member_removed", user_id=user.id, target=project_id,
            ip=client_ip(request), member=member_id,
        )  # fmt: skip

    return router
