"""Invitations to projects (SEC-W1). Adding someone to a project, or making a member an owner,
only offers them the role: they take it by accepting, or decline it. Until then they are not a
member (or keep the role they had), so a project's plan, payments and model calls can never be
put on someone who did not agree — the owner of record only ever moves to an owner."""

import time
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from forge_web.access import RANK
from forge_web.audit import audit
from forge_web.auth.sessions import CurrentUser, client_ip
from forge_web.db.models import Project, ProjectInvitation, ProjectMember, User
from forge_web.services import services_of


async def offer(
    session: AsyncSession, project_id: str, user_id: str, role: str, invited_by: str
) -> None:
    """Offer `user_id` a role in the project (replacing an offer they have not answered yet)."""
    invitation = await session.get(ProjectInvitation, (project_id, user_id))
    if invitation is None:
        invitation = ProjectInvitation(project_id=project_id, user_id=user_id)
        session.add(invitation)
    invitation.role, invitation.invited_by = role, invited_by
    invitation.created_at = time.time()


async def withdraw(session: AsyncSession, project_id: str, user_id: str) -> bool:
    """Take back an open offer; whether there was one."""
    result = await session.execute(
        delete(ProjectInvitation).where(
            ProjectInvitation.project_id == project_id, ProjectInvitation.user_id == user_id
        )
    )
    return bool(result.rowcount)  # type: ignore[attr-defined]


async def pending_invitations(session: AsyncSession, project_id: str) -> list[dict[str, Any]]:
    """The project's open offers, as its owners see them next to the members."""
    rows = await session.execute(
        select(User, ProjectInvitation.role)
        .join(ProjectInvitation, ProjectInvitation.user_id == User.id)
        .where(ProjectInvitation.project_id == project_id)
        .order_by(User.name)
    )
    return [
        {"user_id": u.id, "email": u.email, "name": u.name, "role": role, "pending": True}
        for u, role in rows
    ]


async def no_owner_left(session: AsyncSession, project_id: str) -> HTTPException:
    """The refusal when a change would leave the project without an (accepted) owner."""
    waiting = await session.scalar(
        select(ProjectInvitation.user_id).where(
            ProjectInvitation.project_id == project_id, ProjectInvitation.role == "owner"
        )
    )
    if waiting is not None:
        return HTTPException(
            409, "a project needs at least one owner; the invited owner has not accepted yet"
        )
    return HTTPException(409, "a project needs at least one owner")


async def accept(session: AsyncSession, project_id: str, user: User) -> tuple[str, bool]:
    """Take the offered role: (the role now held, whether the user is a new member)."""
    offered = await session.scalar(
        select(ProjectInvitation.role).where(
            ProjectInvitation.project_id == project_id, ProjectInvitation.user_id == user.id
        )
    )
    # Only one of two answers at the same moment can use the offer up.
    if offered is None or not await withdraw(session, project_id, user.id):
        raise HTTPException(404, "no such invitation")
    member = await session.get(ProjectMember, (project_id, user.id))
    if member is None:
        session.add(ProjectMember(project_id=project_id, user_id=user.id, role=offered))
        return offered, True
    if RANK[offered] > RANK[member.role]:
        member.role = offered
    return member.role, False


def invitations_router() -> APIRouter:
    """`/api/invitations`: the caller's own open offers."""
    router = APIRouter(prefix="/api/invitations")

    @router.get("")
    async def my_invitations(request: Request, user: CurrentUser) -> list[dict[str, Any]]:
        inviter = aliased(User)
        query = (
            select(ProjectInvitation, Project.name, inviter)
            .join(Project, Project.id == ProjectInvitation.project_id)
            .outerjoin(inviter, inviter.id == ProjectInvitation.invited_by)
            .where(ProjectInvitation.user_id == user.id)
            .order_by(ProjectInvitation.created_at.desc())
        )
        async with services_of(request).db.session() as session:
            rows = list(await session.execute(query))
        return [
            {
                "project_id": row[0].project_id,
                "project_name": row[1],
                "role": row[0].role,
                "invited_by": {
                    "name": row[2].name if row[2] else "",
                    "email": row[2].email if row[2] else "",
                },
                "created_at": row[0].created_at,
            }
            for row in rows
        ]

    @router.post("/{project_id}")
    async def accept_invitation(
        project_id: str, request: Request, user: CurrentUser
    ) -> dict[str, Any]:
        services = services_of(request)
        async with services.db.session() as session, session.begin():
            role, joined = await accept(session, project_id, user)
        await audit(
            services.db, "member_added" if joined else "member_role", user_id=user.id,
            target=project_id, ip=client_ip(request), member=user.id, role=role, accepted=True,
        )  # fmt: skip
        return {"project_id": project_id, "role": role}

    @router.delete("/{project_id}", status_code=204)
    async def decline_invitation(project_id: str, request: Request, user: CurrentUser) -> None:
        services = services_of(request)
        async with services.db.session() as session, session.begin():
            if not await withdraw(session, project_id, user.id):
                raise HTTPException(404, "no such invitation")
        await audit(services.db, "invitation_declined", user_id=user.id, target=project_id,
                    ip=client_ip(request))  # fmt: skip

    return router
