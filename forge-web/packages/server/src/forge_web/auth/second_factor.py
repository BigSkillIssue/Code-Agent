"""Two-factor sign-in: a code from an authenticator app after the password or provider sign-in.

With two-factor on, a correct password (or provider sign-in, or reset link) does not start a
session: it leaves a short-lived signed cookie that only `/api/auth/totp/verify` accepts, and
the session starts once a code (or an unused recovery code) is right. Codes are claimed with a
compare-and-set in the database, so two requests cannot use the same code.
"""

import secrets
import time
from typing import Any

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy import update

from forge_web.audit import audit
from forge_web.auth.accounts import refuse_inactive, user_view
from forge_web.auth.sessions import CurrentUser, client_ip, is_https, start_session
from forge_web.auth.totp import code_hash, new_recovery_codes, new_secret, otpauth_uri, verify
from forge_web.db.models import User
from forge_web.services import Services, services_of
from forge_web.vault import sign

PENDING_COOKIE = "forge_2fa"
PENDING_PATH = "/api/auth"
PENDING_SECONDS = 300
ISSUER = "Forge"


class CodeIn(BaseModel):
    """A code from the authenticator app, or a recovery code."""

    code: str = Field(min_length=1, max_length=32)


def pending_key(services: Services) -> bytes:
    """The key that signs pending sign-ins."""
    return services.vault.derive("second-factor")


async def finish_sign_in(request: Request, response: Response, user: User) -> bool:
    """Start a session; with two-factor on, only remember the user until their code (False)."""
    if not user.totp_enabled:
        await start_session(request, response, user)
        return True
    services = services_of(request)
    payload = f"{user.id}.{int(time.time() + PENDING_SECONDS)}"
    response.set_cookie(
        PENDING_COOKIE, f"{payload}.{sign(pending_key(services), payload)}",
        max_age=PENDING_SECONDS, httponly=True, secure=is_https(services), samesite="lax",
        path=PENDING_PATH,
    )  # fmt: skip
    return False


def pending_user(services: Services, value: str | None) -> str | None:
    """The user id of a pending sign-in cookie we signed that has not expired."""
    payload, _, signature = (value or "").rpartition(".")
    if not payload or not secrets.compare_digest(sign(pending_key(services), payload), signature):
        return None
    user_id, _, expires = payload.partition(".")
    return user_id if expires.isdigit() and int(expires) > time.time() else None


async def use_code(services: Services, user: User, code: str) -> str | None:
    """Claim a right code of this user: "totp" or "recovery"; None if it is not right."""
    if not user.totp_enabled or not user.totp_secret:
        return None
    secret = services.vault.decrypt(user.totp_secret)
    step = verify(secret, code, time.time(), user.totp_last_step)
    async with services.db.session() as session, session.begin():
        if step is not None:
            claimed = await session.execute(
                update(User).where(User.id == user.id, User.totp_last_step < step)
                .values(totp_last_step=step)
            )  # fmt: skip
            return "totp" if claimed.rowcount == 1 else None  # type: ignore[attr-defined]
        hashes = user.recovery_codes.split()
        wanted = code_hash(code)
        if not any(secrets.compare_digest(wanted, h) for h in hashes):
            return None
        left = "\n".join(h for h in hashes if h != wanted)
        claimed = await session.execute(
            update(User).where(User.id == user.id, User.recovery_codes == user.recovery_codes)
            .values(recovery_codes=left)
        )  # fmt: skip
        return "recovery" if claimed.rowcount == 1 else None  # type: ignore[attr-defined]


async def load_user(services: Services, user_id: str) -> User | None:
    """The account, fresh from the database."""
    async with services.db.session() as session:
        return await session.get(User, user_id)


def second_factor_router() -> APIRouter:
    """`/api/auth/totp/verify` and `/api/me/totp/*`."""
    router = APIRouter(prefix="/api")

    @router.post("/auth/totp/verify")
    async def verify_sign_in(body: CodeIn, request: Request, response: Response) -> dict[str, Any]:
        services = services_of(request)
        user_id = pending_user(services, request.cookies.get(PENDING_COOKIE))
        if user_id is None:
            raise HTTPException(400, "sign in with your password or provider first")
        if not services.limits.code_by_user.allow(user_id):
            raise HTTPException(429, "too many attempts; try again later")
        user = await load_user(services, user_id)
        how = await use_code(services, user, body.code) if user is not None else None
        if user is None or how is None:
            await audit(services.db, "totp_failed", user_id=user_id, ip=client_ip(request))
            raise HTTPException(401, "that code is not right")
        refuse_inactive(user)
        response.delete_cookie(PENDING_COOKIE, path=PENDING_PATH)
        await start_session(request, response, user)
        await audit(services.db, "login", user_id=user.id, ip=client_ip(request), method=how)
        return user_view(user, services)

    setup_routes(router)
    return router


def setup_routes(router: APIRouter) -> None:
    """Turning two-factor on and off for oneself."""

    @router.post("/me/totp/setup")
    async def setup(request: Request, user: CurrentUser) -> dict[str, str]:
        services = services_of(request)
        if user.totp_enabled:
            raise HTTPException(409, "two-factor sign-in is on already")
        secret = new_secret()
        async with services.db.session() as session, session.begin():
            row = await session.get(User, user.id)
            assert row is not None
            row.totp_secret, row.totp_last_step = services.vault.encrypt(secret), 0
        return {"secret": secret, "uri": otpauth_uri(secret, user.email or user.id, ISSUER)}

    @router.post("/me/totp/enable")
    async def enable(body: CodeIn, request: Request, user: CurrentUser) -> dict[str, Any]:
        services = services_of(request)
        if user.totp_enabled or not user.totp_secret:
            raise HTTPException(409, "start the setup first")
        if not services.limits.code_by_user.allow(user.id):
            raise HTTPException(429, "too many attempts; try again later")
        step = verify(services.vault.decrypt(user.totp_secret), body.code, time.time(), 0)
        if step is None:
            raise HTTPException(400, "that code is not right; is the time on your phone right?")
        codes = new_recovery_codes()
        async with services.db.session() as session, session.begin():
            row = await session.get(User, user.id)
            assert row is not None
            row.totp_enabled, row.totp_last_step = True, step
            row.recovery_codes = "\n".join(code_hash(c) for c in codes)
        await audit(services.db, "totp_enabled", user_id=user.id, ip=client_ip(request))
        return {"recovery_codes": codes}

    @router.post("/me/totp/disable")
    async def disable(body: CodeIn, request: Request, user: CurrentUser) -> dict[str, bool]:
        services = services_of(request)
        if not user.totp_enabled:
            raise HTTPException(409, "two-factor sign-in is off already")
        if user.role == "admin" and services.settings.auth.admin_two_factor:
            raise HTTPException(409, "this server requires two-factor sign-in for admins")
        if not services.limits.code_by_user.allow(user.id):
            raise HTTPException(429, "too many attempts; try again later")
        if await use_code(services, user, body.code) is None:
            raise HTTPException(400, "that code is not right")
        await turn_off(services, user.id)
        await audit(services.db, "totp_disabled", user_id=user.id, ip=client_ip(request))
        return {"totp_enabled": False}


async def turn_off(services: Services, user_id: str) -> None:
    """Remove a user's second factor (secret and recovery codes)."""
    async with services.db.session() as session, session.begin():
        await session.execute(
            update(User).where(User.id == user_id)
            .values(totp_enabled=False, totp_secret=None, totp_last_step=0, recovery_codes="")
        )  # fmt: skip
