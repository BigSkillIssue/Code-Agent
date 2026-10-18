"""A project's one chat (W44): made with the project, on the account of its owner of record,
and made again empty when someone clears the conversation."""

import secrets
import time

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from forge_web.db.models import Chat, Project, User


async def new_chat(
    session: AsyncSession, project: Project, mode: str = "edits", model: str = ""
) -> Chat:
    """An empty chat for the project, on its owner's account (not added to the session)."""
    owner = await session.get(User, project.owner_id)
    now = time.time()
    return Chat(
        id=secrets.token_hex(16), project_id=project.id, user_id=project.owner_id,
        title="New chat", mode=mode, model=model or (owner.default_model if owner else ""),
        state="idle", shared=True, created_at=now, updated_at=now,
    )  # fmt: skip


async def find_chat(session: AsyncSession, project_id: str) -> Chat | None:
    """The project's chat, if it has one yet."""
    return await session.scalar(select(Chat).where(Chat.project_id == project_id))


async def project_chat(session: AsyncSession, project: Project) -> Chat:
    """The project's chat; made the first time (projects from before W44 may have none)."""
    chat = await find_chat(session, project.id)
    if chat is not None:
        return chat
    chat = await new_chat(session, project)
    try:
        async with session.begin_nested():
            session.add(chat)
    except IntegrityError:  # another request made it at the same moment
        found = await find_chat(session, project.id)
        assert found is not None
        return found
    return chat


async def follow_owner(session: AsyncSession, project_id: str, owner_id: str) -> None:
    """The chat's runs use the account of the project's (new) owner of record."""
    await session.execute(
        update(Chat).where(Chat.project_id == project_id).values(user_id=owner_id)
    )
