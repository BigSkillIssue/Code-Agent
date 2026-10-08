"""Sign-in endpoints: first admin, password sign-in and sign-up, sign-out, resets, email checks."""

import re
import secrets
import time
from typing import Any

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy import func, select

from forge_web.audit import audit
from forge_web.auth import onetime
from forge_web.auth.mail import mail_enabled, send_mail
from forge_web.auth.passwords import hash_password, password_problem, verify_password
from forge_web.auth.ratelimit import RateLimiter
from forge_web.auth.sessions import (
    CurrentUser,
    clear_cookies,
    client_ip,
    end_sessions,
    start_session,
)
from forge_web.db.models import User
from forge_web.services import Services, services_of

EMAIL = re.compile(r"^[^@\s]{1,64}@[^@\s]{1,255}\.[^@\s]{2,}$")


class LoginIn(BaseModel):
    """Email and password."""

    email: str = Field(max_length=320)
    password: str = Field(max_length=256)


class SignupIn(LoginIn):
    """A new account (with an invite token in invite-only mode)."""

    name: str = Field(default="", max_length=200)
    invite: str = Field(default="", max_length=200)


class SetupIn(SignupIn):
    """The first admin, with the setup token printed at the first start."""

    token: str = Field(max_length=200)


class TokenIn(BaseModel):
    """A one-time token from a link."""

    token: str = Field(max_length=200)


class ResetIn(TokenIn):
    """A new password with a reset token."""

    password: str = Field(max_length=256)


class EmailIn(BaseModel):
    """An email address."""

    email: str = Field(max_length=320)


def normal_email(email: str) -> str:
    """The email lower-cased; 422 if it does not look like one."""
    email = email.strip().lower()
    if not EMAIL.match(email):
        raise HTTPException(422, "that does not look like an email address")
    return email


def check_password(password: str) -> None:
    """422 with the reason if the password is too weak."""
    problem = password_problem(password)
    if problem:
        raise HTTPException(422, f"password: {problem}")


def limited(limiter: RateLimiter, key: str) -> None:
    """429 when a rate limit is exceeded."""
    if not limiter.allow(key):
        raise HTTPException(429, "too many attempts; try again later")


def user_view(user: User) -> dict[str, Any]:
    """The signed-in user as the web UI sees them."""
    return {"id": user.id, "email": user.email, "name": user.name, "role": user.role,
            "avatar_url": user.avatar_url}  # fmt: skip


async def user_count(services: Services) -> int:
    """How many accounts exist."""
    async with services.db.session() as session:
        return int(await session.scalar(select(func.count()).select_from(User)) or 0)


async def find_user(services: Services, email: str) -> User | None:
    """The account with this email."""
    async with services.db.session() as session:
        return await session.scalar(select(User).where(User.email == email))


def domain_allowed(services: Services, email: str) -> bool:
    """The email's domain may sign up on its own."""
    domains = [d.lower().lstrip("@") for d in services.settings.auth.allowed_domains]
    return not domains or email.rsplit("@", 1)[1] in domains


def auth_router() -> APIRouter:
    """`/api/auth/*` and `/api/me`."""
    router = APIRouter(prefix="/api")

    @router.get("/auth/config")
    async def config(request: Request) -> dict[str, Any]:
        services = services_of(request)
        auth = services.settings.auth
        return {
            "setup_needed": await user_count(services) == 0,
            "passwords": auth.passwords,
            "signup": auth.signup,
            "providers": services.sign_in.buttons(),
            "mail": mail_enabled(auth.smtp),
            "dev": services.settings.dev.enabled,
        }

    @router.get("/me")
    async def me(user: CurrentUser) -> dict[str, Any]:
        return user_view(user)

    @router.post("/auth/setup")
    async def setup(body: SetupIn, request: Request, response: Response) -> dict[str, Any]:
        services = services_of(request)
        limited(services.limits.signup_by_ip, client_ip(request))
        if await user_count(services) > 0 or not services.setup_token:
            raise HTTPException(409, "this server is already set up")
        if not secrets.compare_digest(body.token, services.setup_token):
            raise HTTPException(403, "the setup token is not right")
        email = normal_email(body.email)
        check_password(body.password)
        user = await create_user(
            services, email, body.name, body.password, role="admin", verified=True
        )
        services.setup_token = ""
        await start_session(request, response, user)
        await audit(services.db, "setup", user_id=user.id, ip=client_ip(request))
        return user_view(user)

    @router.post("/auth/login")
    async def login(body: LoginIn, request: Request, response: Response) -> dict[str, Any]:
        services = services_of(request)
        email, ip = body.email.strip().lower(), client_ip(request)
        limited(services.limits.login_by_ip, ip)
        limited(services.limits.login_by_email, email)
        user = await find_user(services, email)
        if not await verify_password(user.password_hash if user else None, body.password):
            await audit(services.db, "login_failed", target=email, ip=ip)
            raise HTTPException(401, "wrong email or password")
        assert user is not None
        refuse_inactive(user)
        await start_session(request, response, user)
        await audit(services.db, "login", user_id=user.id, ip=ip, method="password")
        return user_view(user)

    @router.post("/auth/signup", status_code=201)
    async def signup(body: SignupIn, request: Request, response: Response) -> dict[str, Any]:
        services = services_of(request)
        if not services.settings.auth.passwords:
            raise HTTPException(403, "password accounts are turned off")
        limited(services.limits.signup_by_ip, client_ip(request))
        email = normal_email(body.email)
        check_password(body.password)
        user = await signup_user(services, email, body)
        await audit(
            services.db, "signup", user_id=user.id, ip=client_ip(request), status=user.status
        )
        if user.status == "active":
            await start_session(request, response, user)
        elif user.status == "unverified":
            await send_verification(services, user)
        return {**user_view(user), "status": user.status}

    @router.post("/auth/logout", status_code=204)
    async def logout(request: Request, response: Response, user: CurrentUser) -> None:
        services = services_of(request)
        await end_session_row(services, request)
        clear_cookies(response)
        await audit(services.db, "logout", user_id=user.id, ip=client_ip(request))

    @router.post("/auth/reset")
    async def reset(body: ResetIn, request: Request, response: Response) -> dict[str, Any]:
        services = services_of(request)
        check_password(body.password)
        row = await onetime.consume(services.db, body.token, "reset")
        if row is None or row.user_id is None:
            raise HTTPException(400, "this reset link is not valid (any more)")
        new_hash = await hash_password(body.password)
        async with services.db.session() as session, session.begin():
            user = await session.get(User, row.user_id)
            if user is None:
                raise HTTPException(400, "this reset link is not valid (any more)")
            user.password_hash = new_hash
        await end_sessions(services, user.id)
        refuse_inactive(user)
        await start_session(request, response, user)
        await audit(services.db, "password_reset", user_id=user.id, ip=client_ip(request))
        return user_view(user)

    @router.post("/auth/forgot", status_code=202)
    async def forgot(body: EmailIn, request: Request) -> dict[str, bool]:
        services = services_of(request)
        email = body.email.strip().lower()
        limited(services.limits.mail_by_email, email)
        user = await find_user(services, email)
        if user is not None and mail_enabled(services.settings.auth.smtp):
            token = await onetime.issue(services.db, "reset", user_id=user.id)
            link = f"{services.settings.base_url()}/reset#token={token}"
            text = f"Open this link within 2 hours to choose a new password:\n\n{link}\n"
            await send_mail(services.settings.auth.smtp, email, "Reset your Forge password", text)
        return {"ok": True}  # the same answer whether or not the account exists

    @router.post("/auth/verify")
    async def verify(body: TokenIn, request: Request, response: Response) -> dict[str, Any]:
        services = services_of(request)
        row = await onetime.consume(services.db, body.token, "verify")
        if row is None or row.user_id is None:
            raise HTTPException(400, "this link is not valid (any more)")
        async with services.db.session() as session, session.begin():
            user = await session.get(User, row.user_id)
            if user is None:
                raise HTTPException(400, "this link is not valid (any more)")
            user.email_verified = True
            if user.status == "unverified":
                user.status = "pending" if services.settings.auth.signup == "approval" else "active"
        if user.status == "active":
            await start_session(request, response, user)
        return {**user_view(user), "status": user.status}

    @router.get("/auth/invite/{token}")
    async def invite_info(token: str, request: Request) -> dict[str, Any]:
        row = await onetime.peek(services_of(request).db, token, "invite")
        if row is None:
            raise HTTPException(404, "this invite is not valid (any more)")
        return {"email": row.email, "role": row.role}

    return router


def refuse_inactive(user: User) -> None:
    """403 with the reason for accounts that may not sign in."""
    reasons = {
        "pending": "your account is waiting for an admin to approve it",
        "unverified": "please confirm your email address first (see the mail we sent)",
        "disabled": "this account is disabled",
    }
    if user.status in reasons:
        raise HTTPException(403, reasons[user.status])


async def create_user(
    services: Services,
    email: str,
    name: str,
    password: str | None,
    *,
    role: str = "member",
    status: str = "active",
    verified: bool = False,
) -> User:
    """Store a new account; 409 if the email is taken."""
    if await find_user(services, email) is not None:
        raise HTTPException(409, "an account with this email exists already")
    user = User(
        id=secrets.token_hex(16), email=email, name=name.strip() or email.split("@")[0],
        role=role, status=status, created_at=time.time(), email_verified=verified,
        password_hash=await hash_password(password) if password else None,
    )  # fmt: skip
    async with services.db.session() as session, session.begin():
        session.add(user)
    return user


async def signup_user(services: Services, email: str, body: SignupIn) -> User:
    """Create the account a password sign-up may have."""
    return await new_account(services, email, body.name, body.password, body.invite)


async def new_account(
    services: Services,
    email: str,
    name: str,
    password: str | None,
    invite: str,
    *,
    verified: bool = False,
) -> User:
    """Create the account the sign-up mode allows (`verified`: a provider checked the email)."""
    auth = services.settings.auth
    if invite:
        # Check before using it up: a wrong email must not burn someone else's invite.
        row = await onetime.peek(services.db, invite, "invite")
        if row is None or (row.email and row.email != email):
            raise HTTPException(403, "this invite is not valid for this email")
        row = await onetime.consume(services.db, invite, "invite")
        if row is None:
            raise HTTPException(403, "this invite is not valid for this email")
        return await create_user(services, email, name, password, role=row.role,
                                 verified=verified or bool(row.email))  # fmt: skip
    if auth.signup == "invite" or services.settings.sandbox.isolation == "local":
        raise HTTPException(403, "signing up needs an invite from an admin")
    if not domain_allowed(services, email):
        raise HTTPException(403, "this email domain may not sign up here")
    status = "pending" if auth.signup == "approval" else "active"
    if not verified and mail_enabled(auth.smtp):
        status = "unverified"
    return await create_user(services, email, name, password, status=status, verified=verified)


async def send_verification(services: Services, user: User) -> None:
    """Mail a link that confirms the user's address."""
    token = await onetime.issue(services.db, "verify", user_id=user.id)
    link = f"{services.settings.base_url()}/verify#token={token}"
    text = f"Open this link to confirm your address:\n\n{link}\n"
    await send_mail(services.settings.auth.smtp, user.email or "", "Confirm your email", text)


async def end_session_row(services: Services, request: Request) -> None:
    """End the session of this request."""
    from forge_web.db.models import AuthSession

    session_id = getattr(request.state, "session_id", None)
    if session_id:
        async with services.db.session() as session, session.begin():
            row = await session.get(AuthSession, session_id)
            if row is not None:
                await session.delete(row)
