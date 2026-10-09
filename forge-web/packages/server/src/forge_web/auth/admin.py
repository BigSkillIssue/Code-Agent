"""Administration: accounts (approve, roles, disable), invites, reset links, the audit log —
and every user's own sessions."""

import time
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import delete, func, select

from forge_web.audit import audit
from forge_web.auth import onetime
from forge_web.auth.sessions import AdminUser, CurrentUser, client_ip, end_sessions
from forge_web.db.models import AuditEntry, AuthSession, OneTimeToken, User
from forge_web.members import stop_chats
from forge_web.services import Services, services_of

SHORT_ID = 16  # invites and sessions are named by the start of their hash


class UserPatch(BaseModel):
    """Changes to an account."""

    role: Literal["admin", "member"] | None = None
    status: Literal["active", "disabled"] | None = None


class InviteIn(BaseModel):
    """An invite, optionally for one email address."""

    email: str = Field(default="", max_length=320)
    role: Literal["admin", "member"] = "member"


def account_view(user: User) -> dict[str, Any]:
    """An account as admins see it."""
    return {
        "id": user.id,
        "email": user.email,
        "name": user.name,
        "role": user.role,
        "status": user.status,
        "created_at": user.created_at,
        "has_password": user.password_hash is not None,
        "totp_enabled": user.totp_enabled,
        "email_verified": user.email_verified,
    }


def invite_view(row: OneTimeToken) -> dict[str, Any]:
    """An open invite (never its secret)."""
    return {"id": row.id[:SHORT_ID], "email": row.email, "role": row.role,
            "expires_at": row.expires_at}  # fmt: skip


def session_view(row: AuthSession, current: str) -> dict[str, Any]:
    """One of the user's signed-in browsers."""
    return {"id": row.id[:SHORT_ID], "current": row.id == current, "ip": row.ip,
            "created_at": row.created_at, "last_seen_at": row.last_seen_at,
            "user_agent": row.user_agent}  # fmt: skip


def audit_view(row: AuditEntry) -> dict[str, Any]:
    """One audit entry."""
    return {"at": row.created_at, "user_id": row.user_id, "action": row.action,
            "target": row.target, "detail": row.detail, "ip": row.ip}  # fmt: skip


def short_id(value: str) -> str:
    """A shortened id from a URL; 404 if it is too short to name one row."""
    if len(value) < SHORT_ID:
        raise HTTPException(404, "not found")
    return value


async def other_admins(services: Services, user_id: str) -> int:
    """Active admins besides `user_id`."""
    async with services.db.session() as session:
        count = await session.scalar(
            select(func.count())
            .select_from(User)
            .where(User.role == "admin", User.status == "active", User.id != user_id)
        )
    return int(count or 0)


def admin_router() -> APIRouter:
    """`/api/admin/users|invites|audit` and `/api/auth/sessions`."""
    router = APIRouter(prefix="/api")
    accounts_routes(router)
    invite_routes(router)
    session_routes(router)

    @router.get("/admin/audit")
    async def audit_log(
        request: Request, admin: AdminUser, limit: int = 200
    ) -> list[dict[str, Any]]:
        query = select(AuditEntry).order_by(AuditEntry.id.desc()).limit(min(max(limit, 1), 1000))
        async with services_of(request).db.session() as session:
            return [audit_view(row) for row in await session.scalars(query)]

    return router


def accounts_routes(router: APIRouter) -> None:
    """List, change and approve accounts; reset links."""

    @router.get("/admin/users")
    async def users(request: Request, admin: AdminUser) -> list[dict[str, Any]]:
        async with services_of(request).db.session() as session:
            rows = await session.scalars(select(User).order_by(User.created_at))
            return [account_view(user) for user in rows]

    @router.patch("/admin/users/{user_id}")
    async def change_user(
        user_id: str, body: UserPatch, request: Request, admin: AdminUser
    ) -> dict[str, Any]:
        services = services_of(request)
        changes = body.model_dump(exclude_none=True)
        losing_admin = body.role == "member" or body.status == "disabled"
        if losing_admin and await other_admins(services, user_id) == 0:
            raise HTTPException(409, "the server needs at least one active admin")
        async with services.db.session() as session, session.begin():
            user = await session.get(User, user_id)
            if user is None:
                raise HTTPException(404, "no such account")
            for name, value in changes.items():
                setattr(user, name, value)
        if body.status == "disabled":
            await end_sessions(services, user_id)
            await stop_chats(services, user_id)
        await audit(
            services.db, "user_changed", user_id=admin.id, target=user_id,
            ip=client_ip(request), **changes,
        )  # fmt: skip
        return account_view(user)

    @router.post("/admin/users/{user_id}/approve")
    async def approve(user_id: str, request: Request, admin: AdminUser) -> dict[str, Any]:
        services = services_of(request)
        async with services.db.session() as session, session.begin():
            user = await session.get(User, user_id)
            if user is None or user.status != "pending":
                raise HTTPException(404, "no account waiting for approval")
            user.status = "active"
        await audit(
            services.db, "user_approved", user_id=admin.id, target=user_id, ip=client_ip(request)
        )
        return account_view(user)

    @router.post("/admin/users/{user_id}/reset-link")
    async def reset_link(user_id: str, request: Request, admin: AdminUser) -> dict[str, str]:
        services = services_of(request)
        async with services.db.session() as session:
            if await session.get(User, user_id) is None:
                raise HTTPException(404, "no such account")
        token = await onetime.issue(services.db, "reset", user_id=user_id, created_by=admin.id)
        await audit(
            services.db, "reset_link", user_id=admin.id, target=user_id, ip=client_ip(request)
        )
        return {"link": f"{services.settings.base_url()}/reset#token={token}"}


def invite_routes(router: APIRouter) -> None:
    """Create, list and withdraw invites."""

    @router.post("/admin/invites", status_code=201)
    async def invite(body: InviteIn, request: Request, admin: AdminUser) -> dict[str, str]:
        services = services_of(request)
        email = body.email.strip().lower()
        token = await onetime.issue(
            services.db, "invite", email=email, role=body.role, created_by=admin.id
        )
        await audit(
            services.db, "invite", user_id=admin.id, target=email, ip=client_ip(request),
            role=body.role,
        )  # fmt: skip
        return {"link": f"{services.settings.base_url()}/signup#token={token}"}

    @router.get("/admin/invites")
    async def invites(request: Request, admin: AdminUser) -> list[dict[str, Any]]:
        query = select(OneTimeToken).where(
            OneTimeToken.purpose == "invite",
            OneTimeToken.used_at.is_(None),
            OneTimeToken.expires_at > time.time(),
        )
        async with services_of(request).db.session() as session:
            return [invite_view(row) for row in await session.scalars(query)]

    @router.delete("/admin/invites/{invite_id}", status_code=204)
    async def withdraw(invite_id: str, request: Request, admin: AdminUser) -> None:
        prefix = short_id(invite_id)
        async with services_of(request).db.session() as session, session.begin():
            await session.execute(
                delete(OneTimeToken).where(
                    OneTimeToken.purpose == "invite",
                    OneTimeToken.id.startswith(prefix, autoescape=True),
                )
            )


def session_routes(router: APIRouter) -> None:
    """Every user's own signed-in browsers."""

    @router.get("/auth/sessions")
    async def my_sessions(request: Request, user: CurrentUser) -> list[dict[str, Any]]:
        current = getattr(request.state, "session_id", "")
        query = (
            select(AuthSession)
            .where(AuthSession.user_id == user.id)
            .order_by(AuthSession.last_seen_at.desc())
        )
        async with services_of(request).db.session() as session:
            return [session_view(row, current) for row in await session.scalars(query)]

    @router.delete("/auth/sessions/{session_id}", status_code=204)
    async def end_session(session_id: str, request: Request, user: CurrentUser) -> None:
        prefix = short_id(session_id)
        async with services_of(request).db.session() as session, session.begin():
            await session.execute(
                delete(AuthSession).where(
                    AuthSession.user_id == user.id,
                    AuthSession.id.startswith(prefix, autoescape=True),
                )
            )
