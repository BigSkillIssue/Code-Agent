"""Administration beyond accounts: server settings changed at run time, usage of everyone, key
grants, and turning off a user's lost second factor.

Settings changed here are saved in the database and win over `forge-web.toml` from then on
(also after a restart). Sandbox limits apply to sandboxes started afterwards.
"""

import json
import logging
import time
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator
from sqlalchemy import func, select

from forge_web.audit import audit
from forge_web.auth.second_factor import turn_off
from forge_web.auth.sessions import AdminUser, client_ip, end_sessions
from forge_web.db.engine import Database
from forge_web.db.models import KeyGrant, ServerSetting, UsageRecord, User
from forge_web.gateway.meter import month_start
from forge_web.services import services_of
from forge_web.settings import WebSettings

log = logging.getLogger(__name__)

# Each setting an admin may change, and where it lives in the settings.
FIELDS: dict[str, tuple[str, str]] = {
    "signup": ("auth", "signup"), "allowed_domains": ("auth", "allowed_domains"),
    "passwords": ("auth", "passwords"), "admin_two_factor": ("auth", "admin_two_factor"),
    "projects_per_user": ("quotas", "projects_per_user"),
    "project_disk_mb": ("quotas", "project_disk_mb"),
    "sandbox_cpus": ("sandbox", "cpus"), "sandbox_memory": ("sandbox", "memory"),
    "sandbox_pids": ("sandbox", "pids"), "sandbox_idle_minutes": ("sandbox", "idle_minutes"),
    "server_keys_for": ("gateway", "server_keys_for"),
    "monthly_limit_usd": ("gateway", "monthly_limit_usd"),
    "apple_enabled": ("apple", "enabled"), "apple_allowed": ("apple", "allowed"),
    "apple_minutes_per_month": ("apple", "minutes_per_month"),
}  # fmt: skip


class AdminSettings(BaseModel):
    """Server settings an admin changes; fields left out stay as they are."""

    model_config = ConfigDict(extra="forbid")

    signup: Literal["invite", "approval", "open"] | None = None
    allowed_domains: list[str] | None = Field(default=None, max_length=200)
    passwords: bool | None = None
    admin_two_factor: bool | None = None
    projects_per_user: int | None = Field(default=None, ge=0, le=100_000)
    project_disk_mb: int | None = Field(default=None, ge=0, le=10_000_000)
    sandbox_cpus: float | None = Field(default=None, gt=0, le=256)
    sandbox_memory: str | None = Field(default=None, pattern=r"^[1-9][0-9]{0,6}[kmg]$")
    sandbox_pids: int | None = Field(default=None, ge=64, le=1_000_000)
    sandbox_idle_minutes: float | None = Field(default=None, gt=0, le=525_600)
    server_keys_for: Literal["admins", "granted", "everyone"] | None = None
    monthly_limit_usd: float | None = Field(default=None, ge=0, le=1_000_000)
    apple_enabled: bool | None = None
    apple_allowed: Literal["admins", "granted", "everyone"] | None = None
    apple_minutes_per_month: float | None = Field(default=None, ge=0, le=1_000_000)

    @model_validator(mode="after")
    def no_nulls(self) -> "AdminSettings":
        """A field that is sent must have a value."""
        for name in self.model_fields_set:
            if getattr(self, name) is None:
                raise ValueError(f"{name} needs a value")
        return self


def current(settings: WebSettings) -> dict[str, Any]:
    """Every changeable setting with its value now."""
    return {field: getattr(getattr(settings, section), key)
            for field, (section, key) in FIELDS.items()}  # fmt: skip


def apply(settings: WebSettings, changes: dict[str, Any]) -> None:
    """Use checked values from now on."""
    for field, value in changes.items():
        section, key = FIELDS[field]
        setattr(getattr(settings, section), key, value)


async def load_saved_settings(db: Database, settings: WebSettings) -> None:
    """Apply the settings admins saved (at startup); broken entries are logged and skipped."""
    async with db.session() as session:
        rows = list(await session.scalars(select(ServerSetting)))
    for row in rows:
        try:
            checked = AdminSettings.model_validate({row.key: json.loads(row.value)})
        except (ValueError, ValidationError):
            log.warning("ignoring the saved setting %s: not valid", row.key)
            continue
        apply(settings, checked.model_dump(include=checked.model_fields_set))


def admin_api_router() -> APIRouter:
    """`/api/admin/settings`, `/api/admin/usage`, `/api/admin/grants`, two-factor resets."""
    router = APIRouter(prefix="/api/admin")

    @router.get("/settings")
    async def get_settings(request: Request, admin: AdminUser) -> dict[str, Any]:
        return current(services_of(request).settings)

    @router.patch("/settings")
    async def change_settings(
        body: AdminSettings, request: Request, admin: AdminUser
    ) -> dict[str, Any]:
        services = services_of(request)
        changes = body.model_dump(include=body.model_fields_set)
        now = time.time()
        async with services.db.session() as session, session.begin():
            for field, value in changes.items():
                await session.merge(ServerSetting(key=field, value=json.dumps(value),
                                                  updated_at=now, updated_by=admin.id))  # fmt: skip
        apply(services.settings, changes)
        await audit(services.db, "settings_changed", user_id=admin.id, ip=client_ip(request),
                    changed=sorted(changes))  # fmt: skip
        return current(services.settings)

    @router.get("/usage")
    async def usage(request: Request, admin: AdminUser) -> dict[str, Any]:
        return await usage_this_month(services_of(request).db)

    @router.get("/grants")
    async def grants(request: Request, admin: AdminUser) -> list[dict[str, Any]]:
        async with services_of(request).db.session() as session:
            rows = list(await session.scalars(select(KeyGrant).order_by(KeyGrant.user_id)))
        return [{"user_id": g.user_id, "allowed": g.allowed,
                 "monthly_limit_usd": g.monthly_limit_usd} for g in rows]  # fmt: skip

    @router.post("/users/{user_id}/totp/reset")
    async def reset_two_factor(user_id: str, request: Request, admin: AdminUser) -> dict[str, bool]:
        services = services_of(request)
        async with services.db.session() as session:
            if await session.get(User, user_id) is None:
                raise HTTPException(404, "no such user")
        await turn_off(services, user_id)
        await end_sessions(services, user_id)  # they sign in again, without the old factor
        await audit(services.db, "totp_reset", user_id=admin.id, target=user_id,
                    ip=client_ip(request))  # fmt: skip
        return {"totp_enabled": False}

    return router


async def usage_this_month(db: Database) -> dict[str, Any]:
    """Every user's spending this month, on the server's keys and on their own."""
    start = month_start()
    async with db.session() as session:
        rows = await session.execute(
            select(UsageRecord.user_id, UsageRecord.key_kind, func.sum(UsageRecord.cost_usd),
                   func.sum(UsageRecord.input_tokens), func.sum(UsageRecord.output_tokens),
                   func.count())
            .where(UsageRecord.created_at >= start)
            .group_by(UsageRecord.user_id, UsageRecord.key_kind)
        )  # fmt: skip
        people = {u.id: u for u in await session.scalars(select(User))}
    found: dict[str, dict[str, Any]] = {}
    for user_id, kind, cost, tokens_in, tokens_out, calls in rows:
        person = people.get(user_id)
        row = found.setdefault(user_id, {
            "user_id": user_id, "email": person.email if person else None,
            "name": person.name if person else "", "server_usd": 0.0, "own_usd": 0.0,
            "calls": 0, "input_tokens": 0, "output_tokens": 0,
        })  # fmt: skip
        row["server_usd" if kind == "server" else "own_usd"] += float(cost or 0)
        row["calls"] += int(calls)
        row["input_tokens"] += int(tokens_in or 0)
        row["output_tokens"] += int(tokens_out or 0)
    users = sorted(found.values(), key=lambda r: -(r["server_usd"] + r["own_usd"]))
    total = sum(r["server_usd"] + r["own_usd"] for r in users)
    return {"month_start": start, "total_usd": total, "users": users}
