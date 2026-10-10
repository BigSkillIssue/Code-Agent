"""Each user's App Store Connect team key (W22a): `/api/me/appstore-key`.

The private key (the `AuthKey_….p8` file) is encrypted with the server's vault, never shown
again and never sent to a sandbox or a Mac; only the server signs with it.
"""

import time
from typing import Any

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field

from forge_web.apple.asc_client import AscClient, AscError, AscKey, check_private_key
from forge_web.audit import audit
from forge_web.auth.sessions import CurrentUser, client_ip
from forge_web.db.models import AppStoreKey
from forge_web.services import Services, services_of

UUID = r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"


class AppStoreKeyIn(BaseModel):
    """A team key from App Store Connect (Users and Access → Integrations)."""

    model_config = ConfigDict(extra="forbid")
    key_id: str = Field(pattern=r"^[A-Z0-9]{10}$")
    issuer_id: str = Field(pattern=UUID)
    team_id: str = Field(pattern=r"^[A-Z0-9]{10}$")
    private_key: str = Field(min_length=100, max_length=4000)


def key_view(row: AppStoreKey) -> dict[str, Any]:
    """The key as the user sees it: ids and the last check, never the key itself."""
    return {"key_id": row.key_id, "issuer_id": row.issuer_id, "team_id": row.team_id,
            "created_at": row.created_at, "checked_at": row.checked_at,
            "check_ok": row.check_ok, "check_message": row.check_message}  # fmt: skip


def asc_key_routes() -> APIRouter:
    """Routes for the signed-in user's own key."""
    router = APIRouter(prefix="/api/me/appstore-key")

    @router.get("")
    async def current(request: Request, user: CurrentUser) -> dict[str, Any] | None:
        async with services_of(request).db.session() as session:
            row = await session.get(AppStoreKey, user.id)
        return key_view(row) if row is not None else None

    @router.put("")
    async def save(body: AppStoreKeyIn, request: Request, user: CurrentUser) -> dict[str, Any]:
        try:
            check_private_key(body.private_key)
        except ValueError as err:
            raise HTTPException(422, str(err)) from None
        services = services_of(request)
        secret = services.vault.encrypt(body.private_key.strip() + "\n")
        async with services.db.session() as session, session.begin():
            row = await session.get(AppStoreKey, user.id)
            if row is None:
                row = AppStoreKey(user_id=user.id, created_at=time.time())
                session.add(row)
            row.key_id, row.issuer_id, row.team_id = body.key_id, body.issuer_id, body.team_id
            row.secret, row.created_at = secret, time.time()
        await audit(services.db, "appstore.key_saved", user_id=user.id, ip=client_ip(request),
                    key_id=body.key_id)  # fmt: skip
        return await checked(services, user.id)

    @router.post("/check")
    async def check(request: Request, user: CurrentUser) -> dict[str, Any]:
        return await checked(services_of(request), user.id)

    @router.delete("", status_code=204)
    async def remove(request: Request, user: CurrentUser) -> Response:
        services = services_of(request)
        async with services.db.session() as session, session.begin():
            row = await session.get(AppStoreKey, user.id)
            if row is None:
                raise HTTPException(404, "no App Store Connect key")
            await session.delete(row)
        await audit(services.db, "appstore.key_removed", user_id=user.id, ip=client_ip(request))
        return Response(status_code=204)

    return router


async def asc_client_for(services: Services, user_id: str) -> AscClient | None:
    """A client that signs with this user's key, or None when the user has none."""
    async with services.db.session() as session:
        row = await session.get(AppStoreKey, user_id)
    if row is None:
        return None
    key = AscKey(row.key_id, row.issuer_id, row.team_id, services.vault.decrypt(row.secret))
    return AscClient(services.settings.apple.asc_api_url, key)


async def checked(services: Services, user_id: str) -> dict[str, Any]:
    """Ask Apple whether the key works, keep the answer, and show the key."""
    client = await asc_client_for(services, user_id)
    if client is None:
        raise HTTPException(404, "no App Store Connect key")
    try:
        ok, message = True, await client.check()
    except AscError as err:
        ok, message = False, str(err)
    finally:
        await client.close()
    async with services.db.session() as session, session.begin():
        row = await session.get(AppStoreKey, user_id)
        if row is None:
            raise HTTPException(404, "no App Store Connect key")
        row.checked_at, row.check_ok, row.check_message = time.time(), ok, message[:2000]
        return key_view(row)
