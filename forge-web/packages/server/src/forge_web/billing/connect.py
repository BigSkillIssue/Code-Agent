"""Stripe Connect (W35): the makers of hosted apps take their own customers' money.

A maker connects a Stripe Express account of their own: `POST /api/billing/connect` opens
Stripe's onboarding, where Stripe asks for their identity and bank account (never this server).
Their apps then charge through the payments relay (`billing/relay.py`) as that account, the maker
being the seller, with the admin's application fee (`billing.connect_fee_percent`) on every
charge. The admin's platform key stays on this server. Whether the account may charge and be paid
out is read from Stripe when the maker opens their page, and when a charge needs it.
"""

import time
from typing import Any

from fastapi import APIRouter, HTTPException, Request

from forge_web.audit import audit
from forge_web.auth.sessions import CurrentUser, client_ip
from forge_web.billing.stripe_client import StripeClient, StripeError
from forge_web.db.billing_models import ConnectAccount
from forge_web.db.models import User
from forge_web.services import Services, services_of


async def stripe_of(services: Services) -> StripeClient:
    """Stripe's client with the platform key (409 while payments are not set up)."""
    client = await services.billing.client()
    if client is None or not services.settings.billing.enabled:
        raise HTTPException(409, "payments are not set up on this server")
    return client


async def account_of(services: Services, user_id: str) -> ConnectAccount | None:
    """The user's connected account, as last read from Stripe."""
    async with services.db.session() as session:
        return await session.get(ConnectAccount, user_id)


async def refreshed(
    services: Services, client: StripeClient, row: ConnectAccount
) -> ConnectAccount:
    """The account as Stripe has it now (kept as it was when Stripe cannot say)."""
    try:
        found = await client.get(f"/v1/accounts/{row.stripe_account}")
    except StripeError:
        return row
    async with services.db.session() as session, session.begin():
        kept = await session.get(ConnectAccount, row.user_id)
        if kept is None:
            return row
        kept.charges_enabled = bool(found.get("charges_enabled"))
        kept.payouts_enabled = bool(found.get("payouts_enabled"))
        kept.details_submitted = bool(found.get("details_submitted"))
        kept.checked_at = time.time()
        return kept


async def selling_account(services: Services, user_id: str) -> str:
    """The maker's account that may charge now ("" until Stripe lets it)."""
    row = await account_of(services, user_id)
    if row is None:
        return ""
    if not row.charges_enabled:
        client = await stripe_of(services)
        try:
            row = await refreshed(services, client, row)
        finally:
            await client.close()
    return row.stripe_account if row.charges_enabled else ""


async def onboarding(services: Services, user: User) -> str:
    """Stripe's onboarding page for the user's account (made the first time)."""
    client = await stripe_of(services)
    base = services.settings.base_url()
    try:
        row = await account_of(services, user.id)
        if row is None:
            row = await new_account(services, client, user)
        link = await client.post("/v1/account_links", {
            "account": row.stripe_account, "type": "account_onboarding",
            "refresh_url": f"{base}/billing?connect=again",
            "return_url": f"{base}/billing?connect=done"})  # fmt: skip
    except StripeError as error:
        raise HTTPException(502, f"Stripe: {error}") from None
    finally:
        await client.close()
    return str(link["url"])


async def new_account(services: Services, client: StripeClient, user: User) -> ConnectAccount:
    """A new Express account for the user (once, even when the call is repeated)."""
    made = await client.post("/v1/accounts", {
        "type": "express", "email": user.email or None,
        "capabilities": {"card_payments": {"requested": True}, "transfers": {"requested": True}},
        "metadata": {"forge_user": user.id},
    }, idempotency_key=f"forge-connect-{user.id}")  # fmt: skip
    now = time.time()
    row = ConnectAccount(user_id=user.id, stripe_account=str(made["id"]), charges_enabled=False,
                         payouts_enabled=False, details_submitted=False, created_at=now,
                         checked_at=now)  # fmt: skip
    async with services.db.session() as session, session.begin():
        if await session.get(ConnectAccount, user.id) is None:
            session.add(row)
    return row


def connect_router() -> APIRouter:
    """`/api/billing/connect`: a maker's own connected account."""
    router = APIRouter(prefix="/api/billing/connect")

    @router.get("")
    async def shown(request: Request, user: CurrentUser) -> dict[str, Any]:
        services = services_of(request)
        row = await account_of(services, user.id)
        if row is not None and services.settings.billing.enabled:
            client = await services.billing.client()
            if client is not None:
                try:
                    row = await refreshed(services, client, row)
                finally:
                    await client.close()
        return account_view(services, row)

    @router.post("")
    async def connect(request: Request, user: CurrentUser) -> dict[str, str]:
        services = services_of(request)
        url = await onboarding(services, user)
        await audit(services.db, "billing.connect_started", user_id=user.id,
                    ip=client_ip(request))  # fmt: skip
        return {"url": url}

    @router.post("/dashboard")
    async def dashboard(request: Request, user: CurrentUser) -> dict[str, str]:
        services = services_of(request)
        row = await account_of(services, user.id)
        if row is None or not row.details_submitted:
            raise HTTPException(409, "finish Stripe's onboarding first")
        client = await stripe_of(services)
        try:
            link = await client.post(f"/v1/accounts/{row.stripe_account}/login_links", {})
        except StripeError as error:
            raise HTTPException(502, f"Stripe: {error}") from None
        finally:
            await client.close()
        return {"url": str(link["url"])}

    return router


def account_view(services: Services, row: ConnectAccount | None) -> dict[str, Any]:
    """What the maker sees of their account (not its id: they never need it)."""
    return {"enabled": services.settings.billing.enabled, "connected": row is not None,
            "charges_enabled": bool(row and row.charges_enabled),
            "payouts_enabled": bool(row and row.payouts_enabled),
            "details_submitted": bool(row and row.details_submitted),
            "fee_percent": services.settings.billing.connect_fee_percent}  # fmt: skip
