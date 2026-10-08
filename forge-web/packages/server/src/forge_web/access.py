"""Who may do what with a project or a chat. Not allowed to see it means 404, not 403."""

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from forge_web.db.models import Chat, Project, ProjectMember, User

RANK = {"viewer": 0, "editor": 1, "owner": 2}


async def project_role(session: AsyncSession, user: User, project_id: str) -> str | None:
    """The user's role in the project, or None."""
    member = await session.get(ProjectMember, (project_id, user.id))
    return member.role if member is not None else None


async def require_project(
    session: AsyncSession, user: User, project_id: str, need: str = "viewer"
) -> tuple[Project, str]:
    """The project and the user's role; 404 without access, 403 with too little."""
    project = await session.get(Project, project_id)
    role = await project_role(session, user, project_id) if project is not None else None
    if project is None or role is None:
        raise HTTPException(404, "no such project")
    if RANK[role] < RANK[need]:
        raise HTTPException(403, f"this needs the {need} role in the project")
    return project, role


async def require_chat(
    session: AsyncSession, user: User, chat_id: str, *, write: bool = False
) -> tuple[Chat, Project]:
    """The chat and its project. Reading: the chat's owner, or any member if it is shared.
    Writing (sending, answering, settings): the chat's owner, who must be an editor."""
    chat = await session.get(Chat, chat_id)
    if chat is None:
        raise HTTPException(404, "no such chat")
    project, role = await require_project(session, user, chat.project_id)
    if chat.user_id != user.id and not chat.shared:
        raise HTTPException(404, "no such chat")
    if write and (chat.user_id != user.id or RANK[role] < RANK["editor"]):
        raise HTTPException(403, "only the chat's owner can do that")
    return chat, project


async def member_projects(session: AsyncSession, user: User) -> list[tuple[Project, str]]:
    """Every project the user belongs to, with the role, newest first."""
    rows = await session.execute(
        select(Project, ProjectMember.role)
        .join(ProjectMember, ProjectMember.project_id == Project.id)
        .where(ProjectMember.user_id == user.id)
        .order_by(Project.updated_at.desc())
    )
    return [(row[0], row[1]) for row in rows]
