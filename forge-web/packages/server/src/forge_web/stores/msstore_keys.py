"""Each user's Entra ID app for the Microsoft Store (W38b): `/api/me/msstore-key`.

The maker submits under their own Partner Center account: they register an app in Microsoft
Entra ID, add it in Partner Center as a Manager, and give Forge Web its tenant, client ID and a
client secret. The secret is encrypted with the vault, never shown again and never sent to a
sandbox or a worker; only this server signs in with it.
"""

import time
from typing import Any

from fastapi import APIRouter, HTTPException, Request, Response

from forge_web.audit import audit
from forge_web.auth.sessions import CurrentUser, client_ip
from forge_web.db.store_models import MsStoreKey
from forge_web.services import Services, services_of
from forge_web.stores.msstore import EntraApp, StoreError
from forge_web.stores.msstore_release import store_client_of


def key_view(row: MsStoreKey) -> dict[str, Any]:
    """The app as the user sees it: its ids and the last check, never the secret."""
    return {"tenant_id": row.tenant_id, "client_id": row.client_id,
            "created_at": row.created_at, "checked_at": row.checked_at,
            "check_ok": row.check_ok, "check_message": row.check_message}  # fmt: skip


def msstore_key_routes() -> APIRouter:
    """Routes for the signed-in user's own Entra ID app."""
    router = APIRouter(prefix="/api/me/msstore-key")

    @router.get("")
    async def current(request: Request, user: CurrentUser) -> dict[str, Any] | None:
        async with services_of(request).db.session() as session:
            row = await session.get(MsStoreKey, user.id)
        return key_view(row) if row is not None else None

    @router.put("")
    async def save(body: EntraApp, request: Request, user: CurrentUser) -> dict[str, Any]:
        services = services_of(request)
        secret = services.vault.encrypt(body.model_dump_json())
        async with services.db.session() as session, session.begin():
            row = await session.get(MsStoreKey, user.id)
            if row is None:
                row = MsStoreKey(user_id=user.id, created_at=time.time())
                session.add(row)
            row.tenant_id, row.client_id, row.secret = body.tenant_id, body.client_id, secret
            row.checked_at, row.check_ok, row.check_message = 0.0, False, ""
        await audit(services.db, "stores.msstore_key_saved", user_id=user.id, target=user.id,
                    ip=client_ip(request), client_id=body.client_id)  # fmt: skip
        return key_view(row)

    @router.post("/check")
    async def check(request: Request, user: CurrentUser) -> dict[str, Any]:
        return await checked(services_of(request), user.id)

    @router.delete("")
    async def remove(request: Request, user: CurrentUser) -> Response:
        services = services_of(request)
        async with services.db.session() as session, session.begin():
            row = await session.get(MsStoreKey, user.id)
            if row is not None:
                await session.delete(row)
        await audit(services.db, "stores.msstore_key_removed", user_id=user.id, target=user.id,
                    ip=client_ip(request))  # fmt: skip
        return Response(status_code=204)

    return router


async def checked(services: Services, user_id: str) -> dict[str, Any]:
    """Sign in with the user's app and keep how that went."""
    client = await store_client_of(services.db, services.vault, services.settings, user_id)
    if client is None:
        raise HTTPException(404, "no Entra ID app yet")
    try:
        ok, message = True, await client.check()
    except StoreError as err:
        ok, message = False, f"Microsoft refused the app: {err}"
    finally:
        await client.close()
    async with services.db.session() as session, session.begin():
        row = await session.get(MsStoreKey, user_id)
        if row is None:
            raise HTTPException(404, "no Entra ID app yet")
        row.checked_at, row.check_ok, row.check_message = time.time(), ok, message
    return key_view(row)
