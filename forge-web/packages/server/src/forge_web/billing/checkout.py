"""Checkout (W33a2): customers buy from the catalog through Stripe Checkout, so no card data ever
reaches this server.

The order is placed on Forge Web's own billing page, with the price, the interval and the trial in
view and a button that says the order costs money (§312j BGB). Digital content starts at once, so
the customer must tick that it may begin before the withdrawal period ends and that the right of
withdrawal then lapses (§356 (5) BGB); the order is refused without it, the moment is kept with
the checkout, and the confirmation mail repeats it. Stripe Tax works out the VAT.

Stripe's events then turn into subscriptions (one row per subscription item), which the
entitlements read: created and updated set their state, deleted ends them. A checkout that was
cancelled or expired grants nothing.
"""

import json
import logging
import time
from dataclasses import asdict
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from forge_web.audit import audit
from forge_web.auth.mail import mail_enabled, send_mail
from forge_web.auth.sessions import CurrentUser, client_ip
from forge_web.billing.catalog import item_view
from forge_web.billing.events import Events
from forge_web.billing.stripe_client import StripeClient, StripeError
from forge_web.db.billing_models import BillingCustomer, CatalogItem, Checkout, Subscription
from forge_web.db.engine import Database
from forge_web.db.models import User
from forge_web.legal import require_accepted
from forge_web.services import Services, services_of
from forge_web.settings import WebSettings

log = logging.getLogger(__name__)
WAIVER = ("I want the service to begin before the withdrawal period ends, and I know that my "
          "right of withdrawal then lapses.")  # fmt: skip


class CheckoutIn(BaseModel):
    """What a customer orders."""

    model_config = ConfigDict(extra="forbid")
    item_id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{0,39}$")
    interval: Literal["month", "year", "once"] = "month"
    waiver: bool = False  # the box: begin now, the right of withdrawal lapses


async def customer_of(client: StripeClient, db: Database, user: User) -> str:
    """The user's customer at Stripe (made once)."""
    async with db.session() as session:
        row = await session.get(BillingCustomer, user.id)
    if row is not None:
        return row.stripe_customer
    made = await client.post("/v1/customers", {"email": user.email, "name": user.name,
                                               "metadata": {"forge_user": user.id}},
                             idempotency_key=f"forge-customer-{user.id}")  # fmt: skip
    async with db.session() as session, session.begin():
        if await session.get(BillingCustomer, user.id) is None:
            session.add(BillingCustomer(user_id=user.id, stripe_customer=str(made["id"]),
                                        created_at=time.time()))  # fmt: skip
    return str(made["id"])


async def for_sale(db: Database, item_id: str, interval: str) -> tuple[CatalogItem, str]:
    """The item and its Stripe price for the interval (404, or 409 while it is not for sale)."""
    async with db.session() as session:
        item = await session.get(CatalogItem, item_id)
    if item is None or item.archived_at:
        raise HTTPException(404, "there is no such offer")
    price = json.loads(item.stripe_prices or "{}").get(interval)
    if not price:
        raise HTTPException(409, f"{item.name} is not for sale {interval}ly yet")
    return item, str(price["id"])


def session_params(settings: WebSettings, user: User, item: CatalogItem, price: str,
                   interval: str, customer: str) -> dict[str, Any]:  # fmt: skip
    """What Stripe Checkout gets: the price, the customer, Stripe Tax, where to come back to."""
    base = settings.base_url()
    params: dict[str, Any] = {
        "mode": "payment" if interval == "once" else "subscription",
        "customer": customer, "client_reference_id": user.id,
        "line_items": [{"price": price, "quantity": 1}],
        "success_url": f"{base}/billing?paid={{CHECKOUT_SESSION_ID}}",
        "cancel_url": f"{base}/billing?cancelled=1",
        "automatic_tax": {"enabled": True}, "tax_id_collection": {"enabled": True},
        "customer_update": {"address": "auto", "name": "auto"},
        "allow_promotion_codes": True, "locale": "auto",
        "metadata": {"forge_user": user.id, "forge_item": item.id, "waiver": "given"},
    }  # fmt: skip
    if interval != "once":
        trial = {"trial_period_days": item.trial_days} if item.trial_days else {}
        params["subscription_data"] = {"metadata": {"forge_user": user.id}, **trial}
    return params


async def start_checkout(services: Services, user: User, body: CheckoutIn) -> str:
    """Open a Stripe Checkout for the order; the page to send the customer to."""
    if not body.waiver:
        raise HTTPException(422, "confirm that the service begins now and the right of "
                                 "withdrawal then lapses")  # fmt: skip
    await require_accepted(services.db, services.settings.billing.required_texts, user)
    client = await services.billing.client()
    if client is None or not services.settings.billing.enabled:
        raise HTTPException(409, "payments are not set up on this server")
    item, price = await for_sale(services.db, body.item_id, body.interval)
    try:
        customer = await customer_of(client, services.db, user)
        params = session_params(services.settings, user, item, price, body.interval, customer)
        made = await client.post("/v1/checkout/sessions", params)
    except StripeError as error:
        raise HTTPException(502, f"Stripe: {error}") from None
    finally:
        await client.close()
    now = time.time()
    async with services.db.session() as session, session.begin():
        session.add(Checkout(id=str(made["id"]), user_id=user.id, item_id=item.id,
                             interval=body.interval, waiver_at=now, created_at=now))  # fmt: skip
    return str(made["url"])


def checkout_router() -> APIRouter:
    """`/api/billing`: the offers, one's own subscriptions and limits, and the order."""
    router = APIRouter(prefix="/api/billing")

    @router.get("/catalog")
    async def offers(request: Request, user: CurrentUser) -> list[dict[str, Any]]:
        services = services_of(request)
        async with services.db.session() as session:
            items = await session.scalars(select(CatalogItem).where(
                CatalogItem.archived_at == 0).order_by(CatalogItem.id))  # fmt: skip
            shown = [item_view(i) for i in items if json.loads(i.stripe_prices or "{}")]
        hidden = ("stripe_product", "prices", "synced", "archived_at")
        return [{k: v for k, v in item.items() if k not in hidden} for item in shown]

    @router.get("/me")
    async def mine(request: Request, user: CurrentUser) -> dict[str, Any]:
        services = services_of(request)
        newest_first = Subscription.created_at.desc()
        async with services.db.session() as session:
            rows = list(await session.scalars(select(Subscription).where(
                Subscription.user_id == user.id).order_by(newest_first)))  # fmt: skip
        limits = await services.entitlements.of(user)
        return {"enabled": services.settings.billing.enabled, "entitlements": asdict(limits),
                "subscriptions": [subscription_view(r) for r in rows]}  # fmt: skip

    @router.post("/checkout")
    async def order(body: CheckoutIn, request: Request, user: CurrentUser) -> dict[str, str]:
        services = services_of(request)
        url = await start_checkout(services, user, body)
        await audit(services.db, "billing.checkout_started", user_id=user.id,
                    ip=client_ip(request), item=body.item_id, interval=body.interval,
                    waiver=WAIVER)  # fmt: skip
        return {"url": url}

    return router


class StripeEvents:
    """What Stripe's events change here: checkouts, subscriptions, and the confirmation mail."""

    def __init__(self, db: Database, settings: WebSettings) -> None:
        self.db, self.settings = db, settings

    def register(self, events: Events) -> None:
        events.on("checkout.session.completed", self.completed)
        events.on("checkout.session.expired", self.expired)
        for kind in ("created", "updated", "deleted"):
            events.on(f"customer.subscription.{kind}", self.subscription)

    async def completed(self, event: dict[str, Any]) -> None:
        """A paid order: record it and confirm it by mail, with the waiver."""
        found = await self.mark(event["data"]["object"]["id"], "complete")
        if found is None:
            return
        row, user, item = found
        smtp = self.settings.auth.smtp
        if mail_enabled(smtp) and user.email:
            await send_mail(smtp, user.email, f"Deine Bestellung: {item}", confirmation(item, row))

    async def expired(self, event: dict[str, Any]) -> None:
        """A checkout left without paying: it grants nothing."""
        await self.mark(event["data"]["object"]["id"], "expired")

    async def mark(self, checkout_id: str, status: str) -> tuple[Checkout, User, str] | None:
        async with self.db.session() as session, session.begin():
            row = await session.get(Checkout, checkout_id)
            user = await session.get(User, row.user_id) if row else None
            item = await session.get(CatalogItem, row.item_id) if row else None
            if row is None or user is None:
                log.warning("billing: Stripe names an unknown checkout %s", checkout_id)
                return None
            row.status = status
            row.completed_at = time.time() if status == "complete" else 0.0
        return row, user, item.name if item else row.item_id

    async def subscription(self, event: dict[str, Any]) -> None:
        """A subscription's items as subscriptions here (ended ones too)."""
        sub = event["data"]["object"]
        status = "canceled" if event["type"].endswith(".deleted") else str(sub["status"])
        user_id = await self.user_of(sub)
        if user_id is None:
            log.warning("billing: subscription %s of an unknown customer", sub.get("id"))
            return
        seen = []
        for part in sub.get("items", {}).get("data", []):
            seen.append(str(part["id"]))
            await self.keep(user_id, sub, part, status)
        async with self.db.session() as session, session.begin():
            for row in await session.scalars(select(Subscription).where(
                    Subscription.stripe_subscription == sub["id"])):  # fmt: skip
                if row.id not in seen:
                    row.status, row.updated_at = "canceled", time.time()

    async def user_of(self, sub: dict[str, Any]) -> str | None:
        """Whose subscription it is: its metadata, else its customer."""
        named = sub.get("metadata", {}).get("forge_user")
        async with self.db.session() as session:
            if named and await session.get(User, named) is not None:
                return str(named)
            return await session.scalar(select(BillingCustomer.user_id).where(
                BillingCustomer.stripe_customer == str(sub.get("customer", ""))))  # fmt: skip

    async def keep(self, user_id: str, sub: dict[str, Any], part: dict[str, Any],
                   status: str) -> None:  # fmt: skip
        price = part.get("price", {})
        item_id = str(price.get("metadata", {}).get("forge_item", ""))
        now = time.time()
        async with self.db.session() as session, session.begin():
            row = await session.get(Subscription, str(part["id"]))
            if row is None:
                row = Subscription(id=str(part["id"]), user_id=user_id, item_id=item_id,
                                   status=status, created_at=now, updated_at=now)  # fmt: skip
                session.add(row)
            row.item_id = item_id or row.item_id
            row.status, row.quantity, row.updated_at = status, int(part.get("quantity", 1)), now
            row.interval = str((price.get("recurring") or {}).get("interval", "month"))
            row.stripe_subscription = str(sub["id"])
            row.current_period_end = float(part.get("current_period_end")
                                           or sub.get("current_period_end") or 0)  # fmt: skip


def subscription_view(row: Subscription) -> dict[str, Any]:
    """A subscription as the customer's page shows it."""
    return {"item_id": row.item_id, "status": row.status, "interval": row.interval,
            "quantity": row.quantity, "current_period_end": row.current_period_end}  # fmt: skip


def confirmation(item: str, row: Checkout) -> str:
    """The order's confirmation, with the waiver on a durable medium (§312f BGB)."""
    day = time.strftime("%d.%m.%Y %H:%M UTC", time.gmtime(row.waiver_at))
    return (
        f"Danke für deine Bestellung: {item}.\n\n"
        f"Am {day} hast du ausdrücklich zugestimmt, dass wir vor Ablauf der Widerrufsfrist "
        "mit der Leistung beginnen, und bestätigt, dass dein Widerrufsrecht damit erlischt "
        "(§ 356 Abs. 5 BGB).\n\n"
        "Rechnungen und Zahlungen findest du in deinem Konto unter Abrechnung."
    )
