"""The payments relay (W35): hosted apps take money only through Forge Web, which acts at Stripe
for exactly one connected account, that of the app's maker (Stripe Connect, `billing/connect.py`).

Each app has a token of its own per environment (`billing/relay_tokens.py`), made at its first
deploy that asks for payments (`[payments] kind = "relay"` in forge.app.toml) and sealed into
every release as the secret `PAYMENTS_RELAY_TOKEN`, next to `PAYMENTS_RELAY_URL`. With it the app
opens Stripe Checkout for a one-time payment or a subscription (the admin's application fee on
every charge, the customer sent back only to the app's own addresses), learns how a checkout
ended, and cancels a subscription; it is answered only about the checkouts and subscriptions it
opened itself. The admin's platform key never leaves this server: an answer carries only the
fields an app needs, and Stripe's errors come without the key. A suspended app, or one being
deleted, takes nothing.
"""

import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Annotated, Any, Literal
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import select

from forge_hostworker.wire import app_host
from forge_web.billing.connect import selling_account, stripe_of
from forge_web.billing.relay_tokens import payments_of
from forge_web.billing.stripe_client import StripeError
from forge_web.db.billing_models import RelayCheckout
from forge_web.db.hosting_models import AppDomain, HostedApp
from forge_web.db.models import Project
from forge_web.settings import WebSettings

if TYPE_CHECKING:  # the services hold the deploys, which use the tokens made here
    from forge_web.services import Services

EMAIL = r"^$|^[^@\s<>\",;:]{1,64}@[A-Za-z0-9.-]{1,253}\.[A-Za-z]{2,63}$"


class PaymentIn(BaseModel):
    """What the app's customer buys: once, or every month or year."""

    model_config = ConfigDict(extra="forbid")
    mode: Literal["payment", "subscription"]
    name: str = Field(min_length=1, max_length=200)
    amount: int = Field(ge=50, le=99_999_999)  # cents, per piece
    currency: str = Field(default="eur", pattern=r"^[a-z]{3}$")
    interval: Literal["month", "year"] | None = None  # subscriptions only
    quantity: int = Field(default=1, ge=1, le=100)
    success_url: str = Field(max_length=2000)  # the app's own pages
    cancel_url: str = Field(max_length=2000)
    customer_email: str = Field(default="", max_length=320, pattern=EMAIL)
    reference: str = Field(default="", max_length=200)  # the app's own id for the purchase

    @model_validator(mode="after")
    def interval_fits(self) -> "PaymentIn":
        if (self.mode == "subscription") != (self.interval is not None):
            raise ValueError("a subscription needs its interval, a one-time payment none")
        return self


@dataclass(frozen=True)
class Seller:
    """An app that may take payments, and the one account it takes them for."""

    project_id: str
    environment: str
    app: str
    account: str


def services_of(request: Request) -> "Services":
    """The app's services (as `forge_web.services.services_of`, without importing it)."""
    found: Services = request.app.state.services
    return found


async def current_seller(request: Request) -> Seller:
    """The app of the request's token (401), if it may sell (403) through its maker (409)."""
    services = services_of(request)
    found = await payments_of(services.db, services.vault, request.headers.get("authorization", ""))
    if found is None:
        raise HTTPException(401, "not a valid payments relay token")
    async with services.db.session() as session:
        hosted = await session.get(HostedApp, found.project_id)
        project = await session.get(Project, found.project_id)
    if hosted is None or project is None or hosted.suspended or hosted.deleting_at:
        raise HTTPException(403, "this app may not take payments")
    account = await selling_account(services, project.owner_id)
    if not account:
        raise HTTPException(409, "the app's maker has not finished setting up Stripe payments")
    return Seller(found.project_id, found.environment, hosted.app, account)


SellerOf = Annotated[Seller, Depends(current_seller)]


def payments_router() -> APIRouter:
    """`/api/relay/payments`, for hosted apps."""
    router = APIRouter(prefix="/api/relay/payments")

    @router.post("/checkout")
    async def checkout(body: PaymentIn, seller: SellerOf, request: Request) -> dict[str, str]:
        services = services_of(request)
        allowed = await addresses(services, seller)
        for url in (body.success_url, body.cancel_url):
            if urlsplit(url).scheme != "https" or (urlsplit(url).hostname or "") not in allowed:
                raise HTTPException(422, "customers can only be sent back to the app's own pages")
        made = await at_stripe(services, "POST", "/v1/checkout/sessions", seller,
                               session_params(services.settings, seller, body))  # fmt: skip
        fee = fee_of(services.settings, body)
        async with services.db.session() as session, session.begin():
            session.add(RelayCheckout(id=str(made["id"]), project_id=seller.project_id,
                                      environment=seller.environment,
                                      stripe_account=seller.account, mode=body.mode,
                                      amount=body.amount * body.quantity, currency=body.currency,
                                      fee=fee, reference=body.reference,
                                      created_at=time.time()))  # fmt: skip
        return {"id": str(made["id"]), "url": str(made["url"])}

    @router.get("/checkout/{checkout_id}")
    async def checkout_state(
        checkout_id: str, seller: SellerOf, request: Request
    ) -> dict[str, Any]:
        services = services_of(request)
        row = await opened(services, seller, RelayCheckout.id == checkout_id)
        found = await at_stripe(services, "GET", f"/v1/checkout/sessions/{row.id}", seller)
        subscription = str(found.get("subscription") or "")
        if subscription and subscription != row.subscription:
            async with services.db.session() as session, session.begin():
                kept = await session.get(RelayCheckout, row.id)
                if kept is not None:
                    kept.subscription = subscription
        return checkout_view(found, row)

    subscription_routes(router)
    return router


def subscription_routes(router: APIRouter) -> None:
    """A subscription's state, and its end at the end of the paid period."""

    @router.get("/subscriptions/{subscription_id}")
    async def subscription(
        subscription_id: str, seller: SellerOf, request: Request
    ) -> dict[str, Any]:
        services = services_of(request)
        await opened(services, seller, RelayCheckout.subscription == subscription_id)
        path = f"/v1/subscriptions/{subscription_id}"
        return subscription_view(await at_stripe(services, "GET", path, seller))

    @router.post("/subscriptions/{subscription_id}/cancel")
    async def cancel(subscription_id: str, seller: SellerOf, request: Request) -> dict[str, Any]:
        services = services_of(request)
        await opened(services, seller, RelayCheckout.subscription == subscription_id)
        path = f"/v1/subscriptions/{subscription_id}"
        found = await at_stripe(services, "POST", path, seller, {"cancel_at_period_end": True})
        return subscription_view(found)


async def opened(services: "Services", seller: Seller, which: Any) -> RelayCheckout:
    """A checkout this app opened for this account (404 for anything else)."""
    async with services.db.session() as session:
        row = await session.scalar(select(RelayCheckout).where(
            which, RelayCheckout.project_id == seller.project_id,
            RelayCheckout.environment == seller.environment,
            RelayCheckout.stripe_account == seller.account))  # fmt: skip
    if row is None:
        raise HTTPException(404, "this app opened no such checkout or subscription")
    return row


async def at_stripe(services: "Services", method: str, path: str, seller: Seller,
                    params: dict[str, Any] | None = None) -> dict[str, Any]:  # fmt: skip
    """One call at Stripe as the seller's account."""
    client = await stripe_of(services)
    try:
        return await client.call(method, path, params, account=seller.account)
    except StripeError as error:
        raise HTTPException(502, f"Stripe: {error}") from None
    finally:
        await client.close()


async def addresses(services: "Services", seller: Seller) -> set[str]:
    """The host names of the app in this environment: its own, and its verified domains."""
    async with services.db.session() as session:
        domains = await session.scalars(select(AppDomain.domain).where(
            AppDomain.project_id == seller.project_id,
            AppDomain.environment == seller.environment, AppDomain.verified_at > 0))  # fmt: skip
        found = set(domains)
    apps_domain = services.settings.hosting.apps_domain
    if apps_domain:
        found.add(app_host(seller.app, seller.environment, apps_domain))
    return found


def fee_of(settings: WebSettings, body: PaymentIn) -> int:
    """The admin's fee on a one-time payment, in cents (a subscription's is a percentage)."""
    if body.mode == "subscription":
        return 0
    return round(body.amount * body.quantity * settings.billing.connect_fee_percent / 100)


def session_params(settings: WebSettings, seller: Seller, body: PaymentIn) -> dict[str, Any]:
    """Stripe Checkout for the app's customer, with the admin's fee."""
    recurring = {"interval": body.interval} if body.interval else None
    metadata = {"forge_app": seller.app, "forge_environment": seller.environment}
    params: dict[str, Any] = {
        "mode": body.mode, "success_url": body.success_url, "cancel_url": body.cancel_url,
        "line_items": [{"quantity": body.quantity, "price_data": {
            "currency": body.currency, "unit_amount": body.amount,
            "product_data": {"name": body.name}, "recurring": recurring}}],
        "customer_email": body.customer_email or None,
        "client_reference_id": body.reference or None, "metadata": metadata,
    }  # fmt: skip
    percent = settings.billing.connect_fee_percent
    if body.mode == "subscription":
        params["subscription_data"] = {"metadata": metadata,
                                       "application_fee_percent": percent or None}  # fmt: skip
    else:
        fee = fee_of(settings, body) or None
        params["payment_intent_data"] = {"metadata": metadata, "application_fee_amount": fee}
    return params


def checkout_view(found: dict[str, Any], row: RelayCheckout) -> dict[str, Any]:
    """How a checkout stands, for the app."""
    details = found.get("customer_details") or {}
    return {"id": row.id, "status": found.get("status", ""),
            "payment_status": found.get("payment_status", ""),
            "subscription": found.get("subscription") or "", "reference": row.reference,
            "amount_total": found.get("amount_total"), "currency": row.currency,
            "customer_email": details.get("email") or found.get("customer_email") or "",
            }  # fmt: skip


def subscription_view(found: dict[str, Any]) -> dict[str, Any]:
    """How a subscription stands, for the app."""
    items = (found.get("items") or {}).get("data") or [{}]
    end = found.get("current_period_end") or items[0].get("current_period_end") or 0
    return {"id": found.get("id", ""), "status": found.get("status", ""),
            "cancel_at_period_end": bool(found.get("cancel_at_period_end")),
            "current_period_end": end}  # fmt: skip
