"""Open WebSockets check their access again now and then.

Access is checked when a socket opens or subscribes; these checks repeat it every
RECHECK_SECONDS, so removing someone from a project, demoting them, disabling their account or
ending their sessions also ends the terminals and chat subscriptions they still have open.
"""

from fastapi import HTTPException

from forge_web.access import require_chat, require_project
from forge_web.auth.sessions import load_session
from forge_web.db.models import User
from forge_web.services import Services

RECHECK_SECONDS = 30.0


async def signed_in(services: Services, token: str | None) -> User | None:
    """The user of a session token that is still live (active account), or None."""
    found = await load_session(services, token)
    return found[1] if found is not None else None


async def may_use_project(
    services: Services, token: str | None, project_id: str, need: str
) -> bool:
    """The session is live and its user still has `need` in the project."""
    user = await signed_in(services, token)
    if user is None:
        return False
    async with services.db.session() as session:
        try:
            await require_project(session, user, project_id, need)
        except HTTPException:
            return False
    return True


async def unreadable_chats(services: Services, user: User, chat_ids: set[str]) -> set[str]:
    """The chats among these that the user may no longer read."""
    lost = set()
    async with services.db.session() as session:
        for chat_id in chat_ids:
            try:
                await require_chat(session, user, chat_id)
            except HTTPException:
                lost.add(chat_id)
    return lost
