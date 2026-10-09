"""Links that work once: invites, password resets, email checks. Only their hash is stored."""

import secrets
import time

from sqlalchemy import update

from forge_web.auth.sessions import token_id
from forge_web.db.engine import Database
from forge_web.db.models import OneTimeToken

HOURS = {"invite": 24 * 7, "reset": 2, "verify": 48}


async def issue(
    db: Database,
    purpose: str,
    *,
    user_id: str | None = None,
    email: str = "",
    role: str = "member",
    created_by: str = "",
) -> str:
    """A new token for `purpose`; returns the secret that goes into the link."""
    token, now = secrets.token_urlsafe(32), time.time()
    row = OneTimeToken(
        id=token_id(token), purpose=purpose, user_id=user_id, email=email.lower(), role=role,
        created_by=created_by, created_at=now, expires_at=now + HOURS[purpose] * 3600,
    )  # fmt: skip
    async with db.session() as session, session.begin():
        session.add(row)
    return token


async def revoke(db: Database, purpose: str, user_id: str) -> None:
    """Use up a user's open tokens for `purpose` (old reset links after a new password)."""
    async with db.session() as session, session.begin():
        await session.execute(
            update(OneTimeToken)
            .where(OneTimeToken.purpose == purpose, OneTimeToken.user_id == user_id,
                   OneTimeToken.used_at.is_(None))
            .values(used_at=time.time())
        )  # fmt: skip


async def peek(db: Database, token: str, purpose: str) -> OneTimeToken | None:
    """The token if it is valid for `purpose` and unused (without using it)."""
    async with db.session() as session:
        row = await session.get(OneTimeToken, token_id(token))
    if row is None or row.purpose != purpose or row.used_at is not None:
        return None
    return row if row.expires_at > time.time() else None


async def consume(db: Database, token: str, purpose: str) -> OneTimeToken | None:
    """Use the token (only one caller can): the row, or None if it was not valid."""
    row = await peek(db, token, purpose)
    if row is None:
        return None
    async with db.session() as session, session.begin():
        result = await session.execute(
            update(OneTimeToken)
            .where(OneTimeToken.id == row.id, OneTimeToken.used_at.is_(None))
            .values(used_at=time.time())
        )
        if result.rowcount != 1:  # type: ignore[attr-defined]
            return None
    return row
