"""Stored API keys: a user's own keys win; the server's keys only for users allowed to use them."""

import secrets
import time
from dataclasses import dataclass

from sqlalchemy import delete, select, update

from forge_web.db.engine import Database
from forge_web.db.models import ApiKey, KeyGrant, User
from forge_web.settings import GatewaySettings
from forge_web.vault import Vault


@dataclass(frozen=True)
class ChosenKey:
    """The key a call uses and who pays for it."""

    secret: str | None
    kind: str  # own | server | none
    limit_usd: float | None  # the monthly limit when the server pays


def hint(secret: str) -> str:
    """The last characters of a key, so people can tell keys apart."""
    return f"…{secret[-4:]}" if len(secret) >= 8 else "…"


async def save_key(
    db: Database, vault: Vault, owner_id: str, provider: str, secret: str, name: str = ""
) -> ApiKey:
    """Store a key (replacing the owner's earlier key for the same provider)."""
    key = ApiKey(
        id=secrets.token_hex(16), owner_id=owner_id, provider=provider, name=name or provider,
        hint=hint(secret), secret=vault.encrypt(secret), created_at=time.time(), last_used_at=0,
    )  # fmt: skip
    async with db.session() as session, session.begin():
        await session.execute(
            delete(ApiKey).where(ApiKey.owner_id == owner_id, ApiKey.provider == provider)
        )
        session.add(key)
    return key


async def list_keys(db: Database, owner_id: str) -> list[ApiKey]:
    """The owner's keys ("" = the server's)."""
    async with db.session() as session:
        rows = await session.scalars(
            select(ApiKey).where(ApiKey.owner_id == owner_id).order_by(ApiKey.provider)
        )
        return list(rows)


async def delete_key(db: Database, owner_id: str, key_id: str) -> bool:
    """Delete one of the owner's keys; False if there is none with that id."""
    async with db.session() as session, session.begin():
        result = await session.execute(
            delete(ApiKey).where(ApiKey.owner_id == owner_id, ApiKey.id == key_id)
        )
        return bool(result.rowcount)  # type: ignore[attr-defined]


async def server_key_limit(db: Database, user: User, settings: GatewaySettings) -> float | None:
    """The user's monthly limit on the server's keys, or None if they may not use them."""
    async with db.session() as session:
        grant = await session.get(KeyGrant, user.id)
    if grant is not None and not grant.allowed:
        return None
    allowed = (
        user.role == "admin"
        or settings.server_keys_for == "everyone"
        or (settings.server_keys_for == "granted" and grant is not None)
    )
    if not allowed:
        return None
    if grant is not None and grant.monthly_limit_usd is not None:
        return grant.monthly_limit_usd
    return settings.monthly_limit_usd


async def choose_key(
    db: Database, vault: Vault, user: User, provider: str, settings: GatewaySettings
) -> ChosenKey | None:
    """The key for a call: the user's own, else the server's if allowed, else None."""
    for owner in (user.id, ""):
        if owner == "":
            limit = await server_key_limit(db, user, settings)
            if limit is None:
                return None
        async with db.session() as session:
            key = await session.scalar(
                select(ApiKey).where(ApiKey.owner_id == owner, ApiKey.provider == provider)
            )
        if key is not None:
            async with db.session() as session, session.begin():
                await session.execute(
                    update(ApiKey).where(ApiKey.id == key.id).values(last_used_at=time.time())
                )
            if owner:
                return ChosenKey(vault.decrypt(key.secret), "own", None)
            return ChosenKey(vault.decrypt(key.secret), "server", limit)
    return None
