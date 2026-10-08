"""Development sign-in: one local admin and a secret login link printed at startup.

The link starts an ordinary session for that admin, so everything after it works exactly like a
real sign-in.
"""

import secrets
import time

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select

from forge_web.auth.sessions import start_session
from forge_web.db.engine import Database
from forge_web.db.models import User
from forge_web.services import services_of

DEV_EMAIL = "admin@localhost"


async def ensure_dev_user(db: Database) -> User:
    """The local admin, created on first use."""
    async with db.session() as session, session.begin():
        user = await session.scalar(select(User).where(User.email == DEV_EMAIL))
        if user is None:
            user = User(
                id=secrets.token_hex(16), email=DEV_EMAIL, name="Admin", role="admin",
                status="active", created_at=time.time(), email_verified=True,
            )  # fmt: skip
            session.add(user)
        return user


def dev_router() -> APIRouter:
    """`/api/auth/dev-login` (development mode only)."""
    router = APIRouter()

    @router.get("/api/auth/dev-login")
    async def dev_login(request: Request, token: str) -> RedirectResponse:
        services = services_of(request)
        if not services.dev_token or not secrets.compare_digest(token, services.dev_token):
            raise HTTPException(403, "this login link is not valid")
        async with services.db.session() as session:
            user = await session.get(User, services.dev_user_id)
        if user is None:
            raise HTTPException(403, "this login link is not valid")
        response = RedirectResponse("/", status_code=303)
        await start_session(request, response, user)
        return response

    return router
