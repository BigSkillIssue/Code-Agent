"""Development sign-in: one local admin and a secret login link printed at startup.

Real accounts (passwords, Google, GitHub) replace this in step W08; `current_user` is the one
place that decides who a request comes from.
"""

import secrets
import time
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, WebSocket
from fastapi.responses import RedirectResponse
from sqlalchemy import select

from forge_web.db.engine import Database
from forge_web.db.models import User
from forge_web.services import Services, services_of

COOKIE = "forge_dev"
DEV_EMAIL = "admin@localhost"


async def ensure_dev_user(db: Database) -> User:
    """The local admin, created on first use."""
    async with db.session() as session, session.begin():
        user = await session.scalar(select(User).where(User.email == DEV_EMAIL))
        if user is None:
            user = User(
                id=secrets.token_hex(16),
                email=DEV_EMAIL,
                name="Admin",
                role="admin",
                status="active",
                created_at=time.time(),
            )
            session.add(user)
        return user


async def user_for_token(services: Services, token: str | None) -> User | None:
    """The dev user if the token matches, else None."""
    if not services.dev_token or not token or not secrets.compare_digest(token, services.dev_token):
        return None
    async with services.db.session() as session:
        return await session.get(User, services.dev_user_id)


async def current_user(request: Request) -> User:
    """The signed-in user; 401 otherwise."""
    user = await user_for_token(services_of(request), request.cookies.get(COOKIE))
    if user is None or user.status != "active":
        raise HTTPException(401, "sign in first")
    return user


CurrentUser = Annotated[User, Depends(current_user)]


async def websocket_user(websocket: WebSocket) -> User | None:
    """The signed-in user of a WebSocket connection, or None."""
    return await user_for_token(services_of(websocket), websocket.cookies.get(COOKIE))


def dev_router() -> APIRouter:
    """`/api/auth/dev-login` and `/api/me`."""
    router = APIRouter()

    @router.get("/api/auth/dev-login")
    async def dev_login(request: Request, token: str) -> RedirectResponse:
        services = services_of(request)
        if await user_for_token(services, token) is None:
            raise HTTPException(403, "this login link is not valid")
        response = RedirectResponse("/", status_code=303)
        secure = services.settings.base_url().startswith("https://")
        response.set_cookie(COOKIE, token, httponly=True, samesite="lax", secure=secure)
        return response

    @router.get("/api/me")
    async def me(user: CurrentUser) -> dict[str, object]:
        return {"id": user.id, "email": user.email, "name": user.name, "role": user.role}

    return router
