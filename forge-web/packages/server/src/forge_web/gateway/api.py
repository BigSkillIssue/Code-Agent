"""Keys, providers and usage over HTTP — a user's own, and (for admins) the server's."""

import time
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from forge.providers.catalog import KNOWN_MODELS, PRESETS
from pydantic import BaseModel, Field
from sqlalchemy import func, select

from forge_web.auth.sessions import AdminUser, CurrentUser
from forge_web.db.models import ApiKey, KeyGrant, UsageRecord, User
from forge_web.gateway.keys import delete_key, list_keys, save_key, server_key_limit
from forge_web.gateway.meter import month_start
from forge_web.gateway.upstreams import KINDS
from forge_web.services import Services, services_of


class KeyIn(BaseModel):
    """A key to store."""

    provider: str = Field(min_length=1, max_length=64)
    key: str = Field(min_length=8, max_length=4096)
    name: str = Field(default="", max_length=100)


class GrantIn(BaseModel):
    """Whether a user may use the server's keys, and their monthly limit."""

    allowed: bool = True
    monthly_limit_usd: float | None = Field(default=None, ge=0)


def key_view(key: ApiKey) -> dict[str, Any]:
    """A stored key without its secret."""
    return {"id": key.id, "provider": key.provider, "name": key.name, "hint": key.hint,
            "created_at": key.created_at, "last_used_at": key.last_used_at}  # fmt: skip


# Which vendor serves a catalog model (the catalog lists models without their provider).
MODEL_VENDORS = {"claude-": "anthropic", "gpt-": "openai", "gemini-": "google",
                 "deepseek-": "deepseek", "llama-": "groq"}  # fmt: skip


def model_provider(model: str) -> str | None:
    """The provider that serves a catalog model, or None."""
    return next((p for prefix, p in MODEL_VENDORS.items() if model.startswith(prefix)), None)


async def key_sources(services: Services, user: User) -> tuple[set[str], set[str]]:
    """Providers the user has an own key for, and those they may use the server's key for."""
    own = {k.provider for k in await list_keys(services.db, user.id)}
    limit = await server_key_limit(services.db, user, services.settings.gateway)
    server = {k.provider for k in await list_keys(services.db, "")} if limit is not None else set()
    return own, server


def supported(provider: str) -> bool:
    """The gateway can serve this provider."""
    preset = PRESETS.get(provider)
    return preset is not None and preset.kind in KINDS


async def month_total(services: Services, user_id: str) -> float:
    """The user's spending on the server's keys this month."""
    return await services.gateway.ledger.spent_this_month(user_id)


def keys_router() -> APIRouter:
    """`/api/keys`, `/api/providers`, `/api/usage` and the admin key routes."""
    router = APIRouter()

    @router.get("/api/keys")
    async def own_keys(request: Request, user: CurrentUser) -> list[dict[str, Any]]:
        return [key_view(k) for k in await list_keys(services_of(request).db, user.id)]

    @router.post("/api/keys", status_code=201)
    async def add_key(body: KeyIn, request: Request, user: CurrentUser) -> dict[str, Any]:
        if not supported(body.provider):
            raise HTTPException(422, f"Forge Web cannot use keys for {body.provider}")
        services = services_of(request)
        key = await save_key(
            services.db, services.vault, user.id, body.provider, body.key.strip(), body.name
        )
        return key_view(key)

    @router.delete("/api/keys/{key_id}", status_code=204)
    async def remove_key(key_id: str, request: Request, user: CurrentUser) -> None:
        if not await delete_key(services_of(request).db, user.id, key_id):
            raise HTTPException(404, "no such key")

    @router.get("/api/providers")
    async def providers(request: Request, user: CurrentUser) -> list[dict[str, Any]]:
        own, server = await key_sources(services_of(request), user)
        return [
            {
                "name": name,
                "kind": preset.kind,
                "keyless": preset.api_key_env is None,
                "own_key": name in own,
                "server_key": name in server,
            }
            for name, preset in sorted(PRESETS.items())
            if preset.kind in KINDS
        ]

    @router.get("/api/models")
    async def models(request: Request, user: CurrentUser) -> list[dict[str, Any]]:
        own, server = await key_sources(services_of(request), user)
        found = []
        for model, caps in KNOWN_MODELS.items():
            provider = model_provider(model)
            if provider is None or provider not in own | server:
                continue
            found.append({
                "id": f"{provider}/{model}", "provider": provider, "model": model,
                "key": "own" if provider in own else "server", "cost_in": caps.cost_in,
                "cost_out": caps.cost_out, "context_window": caps.context_window,
            })  # fmt: skip
        return found

    @router.get("/api/usage")
    async def usage(request: Request, user: CurrentUser) -> dict[str, Any]:
        services = services_of(request)
        async with services.db.session() as session:
            rows = await session.execute(
                select(UsageRecord.provider, UsageRecord.model, UsageRecord.key_kind,
                       func.sum(UsageRecord.input_tokens), func.sum(UsageRecord.output_tokens),
                       func.sum(UsageRecord.cost_usd), func.count())
                .where(UsageRecord.user_id == user.id, UsageRecord.created_at >= month_start())
                .group_by(UsageRecord.provider, UsageRecord.model, UsageRecord.key_kind)
            )  # fmt: skip
        by_model = [
            {
                "provider": p,
                "model": m,
                "key_kind": k,
                "input_tokens": int(i or 0),
                "output_tokens": int(o or 0),
                "cost_usd": float(c or 0),
                "calls": int(n),
            }
            for p, m, k, i, o, c, n in rows
        ]
        limit = await server_key_limit(services.db, user, services.settings.gateway)
        return {
            "month_start": month_start(),
            "server_keys_usd": await month_total(services, user.id),
            "limit_usd": limit,
            "by_model": by_model,
        }

    @router.get("/api/admin/keys")
    async def server_keys(request: Request, admin: AdminUser) -> list[dict[str, Any]]:
        return [key_view(k) for k in await list_keys(services_of(request).db, "")]

    @router.post("/api/admin/keys", status_code=201)
    async def add_server_key(body: KeyIn, request: Request, admin: AdminUser) -> dict[str, Any]:
        if not supported(body.provider):
            raise HTTPException(422, f"Forge Web cannot use keys for {body.provider}")
        services = services_of(request)
        key = await save_key(
            services.db, services.vault, "", body.provider, body.key.strip(), body.name
        )
        return key_view(key)

    @router.delete("/api/admin/keys/{key_id}", status_code=204)
    async def remove_server_key(key_id: str, request: Request, admin: AdminUser) -> None:
        if not await delete_key(services_of(request).db, "", key_id):
            raise HTTPException(404, "no such key")

    @router.put("/api/admin/grants/{user_id}")
    async def set_grant(
        user_id: str, body: GrantIn, request: Request, admin: AdminUser
    ) -> dict[str, Any]:
        async with services_of(request).db.session() as session, session.begin():
            if await session.get(User, user_id) is None:
                raise HTTPException(404, "no such user")
            grant = await session.get(KeyGrant, user_id)
            if grant is None:
                grant = KeyGrant(user_id=user_id)
                session.add(grant)
            grant.allowed, grant.monthly_limit_usd = body.allowed, body.monthly_limit_usd
        return {"user_id": user_id, **body.model_dump(), "updated_at": time.time()}

    return router
