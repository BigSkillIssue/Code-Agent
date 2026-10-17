"""Checkout (W33a2): customers buy from the catalog through Stripe Checkout, so no card data ever
reaches this server.

The order is placed on Forge Web's own billing page, with the price, the interval and the trial in
view and a button that says the order costs money (§312j BGB). Digital content starts at once, so
the customer must tick that it may begin before the withdrawal period ends and that the right of
withdrawal then lapses (§356 (5) BGB); the order is refused without it, the moment is kept with
the checkout, and the confirmation mail repeats it. Stripe Tax works out the VAT.

A trial comes with plans only, and only for a customer who never had a plan (W40a); a customer
with a plan changes it in Stripe's portal instead of ordering a second one, and a new order of a
plan ends the customer's other open orders of plans at Stripe, so only one of them can be paid.

Stripe's events then turn into subscriptions (one row per subscription item), which the
entitlements read: created and updated set their state, deleted ends them. A checkout that was
cancelled or expired grants nothing.

An app's one-time price is ordered for one of the customer's own app projects and unlocks it once
paid (W40b, `licenses.py`). A delayed payment method (SEPA) completes the checkout unpaid: it waits
as "pending" until Stripe reports the payment succeeded (then it counts and is confirmed) or
failed (then it never does); a full refund takes the app back.
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
from forge_web.billing.entitlements import PAID
from forge_web.billing.events import Events
from forge_web.billing.licenses import grant, own_app, revoke, unlocked
from forge_web.billing.stripe_client import StripeClient, StripeError
from forge_web.db.billing_models import BillingCustomer, CatalogItem, Checkout, Subscription
from forge_web.db.engine import Database
from forge_web.db.models import Project, User
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
    project_id: str = Field(default="", pattern=r"^([0-9a-f]{16})?$")  # an app's one-time price


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
                   interval: str, customer: str, trial_days: int = 0,
                   project_id: str = "") -> dict[str, Any]:  # fmt: skip
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
    if project_id:
        params["metadata"]["forge_project"] = project_id
    if interval != "once":
        trial = {"trial_period_days": trial_days} if trial_days else {}
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
    try:
        item, price = await for_sale(services.db, body.item_id, body.interval)
        if (item.kind == "app_once") != bool(body.project_id):
            raise HTTPException(422, "an app's one-time price, and only that, names the app")
        if body.project_id:
            await own_app(services.db, user, body.project_id)
        if item.kind == "plan" and await plan_running(services.db, user.id):
            raise HTTPException(409, "you have a plan already; change it in the customer portal")
        trial_days = await trial_for(services.db, user.id, item)
        customer = await customer_of(client, services.db, user)
        if item.kind == "plan":
            await expire_open_plans(client, services.db, user.id)
        params = session_params(services.settings, user, item, price, body.interval, customer,
                                trial_days, body.project_id)  # fmt: skip
        made = await client.post("/v1/checkout/sessions", params)
    except StripeError as error:
        raise HTTPException(502, f"Stripe: {error}") from None
    finally:
        await client.close()
    now = time.time()
    async with services.db.session() as session, session.begin():
        session.add(Checkout(id=str(made["id"]), user_id=user.id, item_id=item.id,
                             interval=body.interval, waiver_at=now, created_at=now,
                             trial_days=trial_days, project_id=body.project_id))  # fmt: skip
    return str(made["url"])


def plan_ids() -> Any:
    """The catalog's plans, for a query."""
    return select(CatalogItem.id).where(CatalogItem.kind == "plan")


async def plan_running(db: Database, user_id: str) -> bool:
    """Whether the user pays for a plan now (or tries one)."""
    async with db.session() as session:
        found = await session.scalar(select(Subscription.id).where(
            Subscription.user_id == user_id, Subscription.status.in_(PAID),
            Subscription.item_id.in_(plan_ids())).limit(1))  # fmt: skip
    return found is not None


async def trial_for(db: Database, user_id: str, item: CatalogItem) -> int:
    """The days of trial an order gets: a plan's, for a customer who never had a plan."""
    if item.kind != "plan" or not item.trial_days:
        return 0
    async with db.session() as session:
        had = await session.scalar(
            select(Subscription.id)
            .where(Subscription.user_id == user_id, Subscription.item_id.in_(plan_ids()))
            .limit(1)
        )
        tried = await session.scalar(select(Checkout.id).where(
            Checkout.user_id == user_id, Checkout.trial_days > 0,
            Checkout.status == "complete").limit(1))  # fmt: skip
    return 0 if had or tried else item.trial_days


async def expire_open_plans(client: StripeClient, db: Database, user_id: str) -> None:
    """End the user's open orders of plans at Stripe, so only the newest can be paid."""
    async with db.session() as session:
        rows = list(await session.scalars(select(Checkout).where(
            Checkout.user_id == user_id, Checkout.status == "open",
            Checkout.item_id.in_(plan_ids()))))  # fmt: skip
    for row in rows:
        try:
            await client.post(f"/v1/checkout/sessions/{row.id}/expire", {})
        except StripeError as error:  # paid or ended already: its own event says which
            log.info("billing: checkout %s not expired: %s", row.id, error)
            continue
        async with db.session() as session, session.begin():
            stored = await session.get(Checkout, row.id)
            if stored is not None and stored.status == "open":
                stored.status = "expired"


def checkout_router() -> APIRouter:
    """`/api/billing`: the offers, one's own subscriptions and limits, and the order."""
    router = APIRouter(prefix="/api/billing")

    @router.get("/catalog")
    async def offers(request: Request, user: CurrentUser) -> list[dict[str, Any]]:
        services = services_of(request)
        async with services.db.session() as session:
            items = list(await session.scalars(select(CatalogItem).where(
                CatalogItem.archived_at == 0).order_by(CatalogItem.id)))  # fmt: skip
        shown = []
        for item in items:
            if json.loads(item.stripe_prices or "{}"):
                view = item_view(item)
                view["trial_days"] = await trial_for(services.db, user.id, item)
                shown.append(view)
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
                "subscriptions": [subscription_view(r) for r in rows],
                "apps": await apps_of(services.db, user.id)}  # fmt: skip

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
        events.on("checkout.session.async_payment_succeeded", self.payment_succeeded)
        events.on("checkout.session.async_payment_failed", self.payment_failed)
        events.on("checkout.session.expired", self.expired)
        events.on("charge.refunded", self.refunded)
        for kind in ("created", "updated", "deleted"):
            events.on(f"customer.subscription.{kind}", self.subscription)

    async def completed(self, event: dict[str, Any]) -> None:
        """An order is through: paid now, or (SEPA) its payment is still under way."""
        order = event["data"]["object"]
        paid = order.get("payment_status", "paid") != "unpaid"
        await self.settle(order, "complete" if paid else "pending")

    async def payment_succeeded(self, event: dict[str, Any]) -> None:
        """A delayed payment arrived: the order counts now."""
        await self.settle(event["data"]["object"], "complete")

    async def payment_failed(self, event: dict[str, Any]) -> None:
        """A delayed payment failed: the order grants nothing."""
        await self.settle(event["data"]["object"], "failed")

    async def settle(self, order: dict[str, Any], status: str) -> None:
        """Record what became of an order; unlock its app once paid, and tell the customer."""
        intent = str(order.get("payment_intent") or "")
        found = await self.mark(str(order["id"]), status, intent)
        if found is None:
            return
        row, user, item = found
        if status == "complete" and row.project_id:
            await grant(self.db, row.project_id, row.user_id, checkout_id=row.id)
        smtp = self.settings.auth.smtp
        if not mail_enabled(smtp) or not user.email or status == "pending":
            return
        if status == "complete":
            await send_mail(smtp, user.email, f"Deine Bestellung: {item}", confirmation(item, row))
        else:
            await send_mail(smtp, user.email, f"Zahlung fehlgeschlagen: {item}",
                            f"Die Zahlung für deine Bestellung {item} ist fehlgeschlagen. Die "
                            "Bestellung gilt nicht; du kannst sie unter Abrechnung neu "
                            "aufgeben.")  # fmt: skip

    async def expired(self, event: dict[str, Any]) -> None:
        """A checkout left without paying: it grants nothing."""
        await self.mark(event["data"]["object"]["id"], "expired")

    async def refunded(self, event: dict[str, Any]) -> None:
        """A one-time payment refunded in full: its app is no longer paid for."""
        charge = event["data"]["object"]
        intent = str(charge.get("payment_intent") or "")
        if not charge.get("refunded") or not intent:
            return  # a partial refund leaves the app unlocked
        async with self.db.session() as session, session.begin():
            row = await session.scalar(select(Checkout).where(Checkout.payment_intent == intent))
            if row is None:
                return
            row.status = "refunded"
        await revoke(self.db, "refunded", checkout_id=row.id)

    async def mark(self, checkout_id: str, status: str,
                   intent: str = "") -> tuple[Checkout, User, str] | None:  # fmt: skip
        async with self.db.session() as session, session.begin():
            row = await session.get(Checkout, checkout_id)
            user = await session.get(User, row.user_id) if row else None
            item = await session.get(CatalogItem, row.item_id) if row else None
            if row is None or user is None:
                log.warning("billing: Stripe names an unknown checkout %s", checkout_id)
                return None
            row.status = status
            row.completed_at = time.time() if status == "complete" else 0.0
            row.payment_intent = intent or row.payment_intent
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


async def apps_of(db: Database, user_id: str) -> list[dict[str, Any]]:
    """The user's own app projects, and whether each is unlocked (W40b)."""
    async with db.session() as session:
        projects = list(await session.scalars(
            select(Project).where(Project.owner_id == user_id, Project.kind == "app")
            .order_by(Project.name)))  # fmt: skip
    return [{"project_id": p.id, "name": p.name,
             "unlocked": await unlocked(db, p.id) is not None} for p in projects]  # fmt: skip
