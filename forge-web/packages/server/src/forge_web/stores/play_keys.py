"""Each user's Google Play service account (W37b): `/api/me/play-key`.

The maker publishes under their own Play developer account: they make a service account in
Google Cloud, invite its email in Play Console with the permission to release, and give Forge
Web its JSON key file. The key is encrypted with the vault, never shown again and never sent to
a sandbox or a host; only this server signs with it. Whether the developer account is a
personal or an organisation one decides whether Google asks for a closed test first.
"""

import time
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, ValidationError

from forge_web.audit import audit
from forge_web.auth.sessions import CurrentUser, client_ip
from forge_web.db.store_models import PlayKey
from forge_web.services import Services, services_of
from forge_web.stores.play import PlayError, ServiceAccount, check_private_key
from forge_web.stores.play_release import play_client_of

CLOSED_TEST = (
    "A personal Google Play developer account made after 13 November 2023 must run a closed "
    "test (the alpha track) with at least 12 testers for 14 days in a row before Google lets "
    "it publish to production. An organisation account (with a D-U-N-S number) does not need it."
)


class PlayKeyIn(BaseModel):
    """A service account's JSON key file, and which kind of developer account it publishes for."""

    model_config = ConfigDict(extra="forbid")
    key_file: dict[str, Any]
    account: Literal["personal", "organisation"]


def key_view(row: PlayKey) -> dict[str, Any]:
    """The key as the user sees it: its email and the last check, never the key itself."""
    return {"client_email": row.client_email, "account": row.account,
            "created_at": row.created_at, "checked_at": row.checked_at,
            "check_ok": row.check_ok, "check_message": row.check_message,
            "notice": CLOSED_TEST if row.account == "personal" else ""}  # fmt: skip


def account_of(key_file: dict[str, Any]) -> ServiceAccount:
    """The service account in a key file; 422 when it is not one."""
    try:
        account = ServiceAccount.model_validate(key_file)
        check_private_key(account.private_key)
    except ValidationError as err:
        problem = err.errors()[0]
        where = ".".join(str(part) for part in problem["loc"])
        raise HTTPException(422, f"not a service account's key file: {where}: "
                                 f"{problem['msg']}") from None  # fmt: skip
    except ValueError as err:
        raise HTTPException(422, str(err)) from None
    return account


def play_key_routes() -> APIRouter:
    """Routes for the signed-in user's own service account."""
    router = APIRouter(prefix="/api/me/play-key")

    @router.get("")
    async def current(request: Request, user: CurrentUser) -> dict[str, Any] | None:
        async with services_of(request).db.session() as session:
            row = await session.get(PlayKey, user.id)
        return key_view(row) if row is not None else None

    @router.put("")
    async def save(body: PlayKeyIn, request: Request, user: CurrentUser) -> dict[str, Any]:
        account = account_of(body.key_file)
        services = services_of(request)
        secret = services.vault.encrypt(account.model_dump_json())
        async with services.db.session() as session, session.begin():
            row = await session.get(PlayKey, user.id)
            if row is None:
                row = PlayKey(user_id=user.id, created_at=time.time())
                session.add(row)
            row.client_email, row.secret, row.account = account.client_email, secret, body.account
            row.checked_at, row.check_ok, row.check_message = 0.0, False, ""
        await audit(services.db, "stores.play_key_saved", user_id=user.id, target=user.id,
                    ip=client_ip(request), client_email=account.client_email)  # fmt: skip
        return key_view(row)

    @router.post("/check")
    async def check(request: Request, user: CurrentUser) -> dict[str, Any]:
        return await checked(services_of(request), user.id)

    @router.delete("")
    async def remove(request: Request, user: CurrentUser) -> Response:
        services = services_of(request)
        async with services.db.session() as session, session.begin():
            row = await session.get(PlayKey, user.id)
            if row is not None:
                await session.delete(row)
        await audit(services.db, "stores.play_key_removed", user_id=user.id, target=user.id,
                    ip=client_ip(request))  # fmt: skip
        return Response(status_code=204)

    return router


async def checked(services: Services, user_id: str) -> dict[str, Any]:
    """Ask Google for a token with the user's key and keep how that went."""
    found = await play_client_of(services.db, services.vault, services.settings, user_id)
    if found is None:
        raise HTTPException(404, "no Google Play service account yet")
    client, _ = found
    try:
        await client.bearer()
        ok, message = True, ("The key works: Google gave it an access token. Whether it may "
                             "publish an app shows at that app's first release.")  # fmt: skip
    except PlayError as err:
        ok, message = False, f"Google refused the key: {err}"
    finally:
        await client.close()
    async with services.db.session() as session, session.begin():
        row = await session.get(PlayKey, user_id)
        if row is None:
            raise HTTPException(404, "no Google Play service account yet")
        row.checked_at, row.check_ok, row.check_message = time.time(), ok, message
    return key_view(row)
