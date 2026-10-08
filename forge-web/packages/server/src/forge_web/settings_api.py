"""A user's own settings: name, default model and password.

API keys (`gateway/api.py`), linked sign-ins (`auth/oauth.py`), git credentials, sessions and
two-factor sign-in (`auth/second_factor.py`) have their own routes; the settings page uses all
of them.
"""

import re
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field, field_validator

from forge_web.audit import audit
from forge_web.auth.accounts import user_view
from forge_web.auth.passwords import hash_password, password_problem, verify_password
from forge_web.auth.sessions import CurrentUser, client_ip, end_sessions
from forge_web.db.models import User
from forge_web.services import services_of

MODEL = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}/[A-Za-z0-9][A-Za-z0-9._:/@-]{0,130}$")


class ProfileIn(BaseModel):
    """Changes to one's profile; fields left out stay as they are."""

    name: str | None = Field(default=None, max_length=200)
    default_model: str | None = Field(default=None, max_length=200)

    @field_validator("default_model")
    @classmethod
    def model_name(cls, value: str | None) -> str | None:
        """Empty (the server's default) or `provider/model`."""
        if value and not MODEL.match(value):
            raise ValueError("use provider/model, e.g. anthropic/claude-sonnet-4-5")
        return value


class PasswordIn(BaseModel):
    """The current password (if there is one) and a new one."""

    current: str = Field(default="", max_length=256)
    new: str = Field(max_length=256)


def settings_router() -> APIRouter:
    """`PATCH /api/me` and `POST /api/me/password`."""
    router = APIRouter(prefix="/api/me")

    @router.patch("")
    async def update_profile(
        body: ProfileIn, request: Request, user: CurrentUser
    ) -> dict[str, Any]:
        services = services_of(request)
        async with services.db.session() as session, session.begin():
            row = await session.get(User, user.id)
            assert row is not None
            if body.name is not None:
                row.name = body.name.strip()
            if body.default_model is not None:
                row.default_model = body.default_model
        return user_view(row, services)

    @router.post("/password")
    async def change_password(
        body: PasswordIn, request: Request, user: CurrentUser
    ) -> dict[str, bool]:
        services = services_of(request)
        if not services.settings.auth.passwords:
            raise HTTPException(403, "password accounts are turned off")
        if not services.limits.login_by_email.allow(user.email or user.id):
            raise HTTPException(429, "too many attempts; try again later")
        if user.password_hash is not None and not await verify_password(
            user.password_hash, body.current
        ):
            raise HTTPException(403, "the current password is not right")
        problem = password_problem(body.new)
        if problem:
            raise HTTPException(422, f"password: {problem}")
        new_hash = await hash_password(body.new)
        async with services.db.session() as session, session.begin():
            row = await session.get(User, user.id)
            assert row is not None
            row.password_hash = new_hash
        await end_sessions(services, user.id, keep=request.state.session_id)
        await audit(services.db, "password_changed", user_id=user.id, ip=client_ip(request))
        return {"ok": True}

    return router
