"""The catalog (W32): what customers can buy, set by the admin and synced to Stripe.

Items are plans (monthly and yearly, with a trial and the limits W33a enforces), a one-time price
per app, hosting per app and month, and add-ons; coupons give a discount with a code. Each item
is a Stripe product with one price per interval. Stripe's prices never change: a new amount is a
new price, and the old one is archived. A removed item is archived at Stripe, never deleted, so
running subscriptions and old invoices keep working. Syncing compares each item with what was
synced last and calls Stripe only for what changed, with idempotency keys made from the change,
so syncing twice (or again after a crash) changes nothing.
"""

import hashlib
import json
import time
from dataclasses import dataclass, field
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import select

from forge_web.billing.stripe_client import StripeClient
from forge_web.db.billing_models import CatalogItem, Coupon
from forge_web.db.engine import Database

Kind = Literal["plan", "app_once", "hosting", "addon"]
INTERVALS: dict[str, dict[str, Any]] = {  # an item's amounts and how Stripe bills each
    "month": {"recurring": {"interval": "month"}},
    "year": {"recurring": {"interval": "year"}},
    "once": {},
}
KIND_INTERVALS: dict[str, tuple[str, ...]] = {
    "plan": ("month", "year"), "app_once": ("once",), "hosting": ("month",), "addon": ("month",),
}  # fmt: skip


class Limits(BaseModel):
    """What a plan allows (W33a enforces it); None: no limit."""

    model_config = ConfigDict(extra="forbid")
    projects: int | None = Field(default=None, ge=0)
    ai_budget_month_eur: float | None = Field(default=None, ge=0)
    ai_budget_per_product_eur: float | None = Field(default=None, ge=0)
    mac_minutes: int | None = Field(default=None, ge=0)
    hosted_apps: int | None = Field(default=None, ge=0)
    storage_gb: float | None = Field(default=None, ge=0)
    custom_domains: int | None = Field(default=None, ge=0)
    store_publishing: bool = False


class ItemIn(BaseModel):
    """An item as the admin sets it (amounts in cents)."""

    model_config = ConfigDict(extra="forbid")
    kind: Kind
    name: str = Field(min_length=1, max_length=200)
    description: str = Field(default="", max_length=2000)
    currency: Literal["eur", "usd", "chf", "gbp"] = "eur"
    amounts: dict[Literal["month", "year", "once"], Annotated[int, Field(gt=0, le=10**8)]] = Field(
        default_factory=dict
    )
    trial_days: int = Field(default=0, ge=0, le=365)
    limits: Limits = Field(default_factory=Limits)

    @model_validator(mode="after")
    def fitting_amounts(self) -> "ItemIn":
        """A plan is paid monthly or yearly, an app once, hosting and add-ons monthly."""
        allowed = KIND_INTERVALS[self.kind]
        if not self.amounts or not set(self.amounts) <= set(allowed):
            raise ValueError(f"a {self.kind} has an amount for {' or '.join(allowed)}")
        return self


class CouponIn(BaseModel):
    """A discount with a code customers type at checkout."""

    model_config = ConfigDict(extra="forbid")
    code: str = Field(pattern=r"^[A-Z0-9_-]{3,40}$")
    name: str = Field(min_length=1, max_length=200)
    percent_off: float | None = Field(default=None, gt=0, le=100)
    amount_off: int | None = Field(default=None, gt=0)  # cents, in `currency`
    currency: Literal["eur", "usd", "chf", "gbp"] = "eur"
    duration: Literal["once", "repeating", "forever"] = "once"
    duration_in_months: int | None = Field(default=None, ge=1, le=36)

    @model_validator(mode="after")
    def one_discount(self) -> "CouponIn":
        """Either a percentage or an amount off; months only for a repeating discount."""
        if (self.percent_off is None) == (self.amount_off is None):
            raise ValueError("a coupon takes either percent_off or amount_off")
        if (self.duration == "repeating") != (self.duration_in_months is not None):
            raise ValueError("duration_in_months goes with a repeating coupon only")
        return self


@dataclass
class SyncReport:
    """What one sync did at Stripe."""

    created: list[str] = field(default_factory=list)
    updated: list[str] = field(default_factory=list)
    archived: list[str] = field(default_factory=list)
    unchanged: int = 0


def fingerprint(data: Any) -> str:
    """A short, stable digest of what was synced."""
    return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()[:24]


class Catalog:
    """The admin's catalog and its sync to Stripe."""

    def __init__(self, db: Database) -> None:
        self.db = db

    async def sync(self, client: StripeClient) -> SyncReport:
        """Bring Stripe in line with the catalog; what that took."""
        report = SyncReport()
        async with self.db.session() as session:
            items = list(await session.scalars(select(CatalogItem).order_by(CatalogItem.id)))
            coupons = list(await session.scalars(select(Coupon).order_by(Coupon.id)))
        for item in items:
            await self.sync_item(client, item, report)
        for coupon in coupons:
            await self.sync_coupon(client, coupon, report)
        return report

    async def sync_item(self, client: StripeClient, item: CatalogItem, report: SyncReport) -> None:
        """One item: its product, then a price per interval."""
        changed = False
        product = {"name": item.name, "description": item.description or None,
                   "active": not item.archived_at}  # fmt: skip
        if not item.stripe_product:
            params = product | {"metadata": {"forge_item": item.id}}
            made = await client.post("/v1/products", params,
                                     idempotency_key=f"forge-product-{item.id}")  # fmt: skip
            item.stripe_product, changed = str(made["id"]), True
            report.created.append(f"product {item.id}")
        elif fingerprint(product) != item.product_synced:
            key = f"forge-product-{item.id}-v{item.version}"
            await client.post(f"/v1/products/{item.stripe_product}", product, idempotency_key=key)
            changed = True
            (report.archived if item.archived_at else report.updated).append(f"product {item.id}")
        item.product_synced = fingerprint(product)
        prices = json.loads(item.stripe_prices or "{}")
        changed = await self.sync_prices(client, item, prices, report) or changed
        if not changed:
            report.unchanged += 1
        if changed or item.synced_at < item.updated_at:  # limits and trials live here only
            await self.save_item(item, prices)

    async def sync_prices(self, client: StripeClient, item: CatalogItem, prices: dict[str, Any],
                          report: SyncReport) -> bool:  # fmt: skip
        """A price per interval; a changed amount is a new price. Whether any changed."""
        amounts = {} if item.archived_at else json.loads(item.amounts or "{}")
        changed = False
        for interval in KIND_INTERVALS[item.kind]:
            amount, current = amounts.get(interval), prices.get(interval)
            if current and (current["amount"], current["currency"]) == (amount, item.currency):
                continue
            if amount is None and current is None:
                continue
            if amount is not None:
                params = {"product": item.stripe_product, "unit_amount": amount,
                          "currency": item.currency, "metadata": {"forge_item": item.id},
                          **INTERVALS[interval]}  # fmt: skip
                key = f"forge-price-{item.id}-v{item.version}-{interval}"
                made = await client.post("/v1/prices", params, idempotency_key=key)
                prices[interval] = {"id": made["id"], "amount": amount, "currency": item.currency}
                report.created.append(f"price {item.id}/{interval}")
            else:
                prices.pop(interval, None)
            if current:  # prices never change at Stripe: the old one is archived
                await client.post(f"/v1/prices/{current['id']}", {"active": False},
                                  idempotency_key=f"forge-archive-{current['id']}")  # fmt: skip
                report.archived.append(f"price {item.id}/{interval}")
            changed = True
        return changed

    async def save_item(self, item: CatalogItem, prices: dict[str, Any]) -> None:
        """Remember what Stripe has now."""
        async with self.db.session() as session, session.begin():
            row = await session.get(CatalogItem, item.id)
            if row is not None:
                row.stripe_product, row.product_synced = item.stripe_product, item.product_synced
                row.stripe_prices, row.synced_at = json.dumps(prices), time.time()

    async def sync_coupon(self, client: StripeClient, coupon: Coupon, report: SyncReport) -> None:
        """A coupon and its code; a removed one's code stops working (the coupon stays)."""
        if not coupon.stripe_coupon and not coupon.archived_at:
            terms: dict[str, Any] = {"id": f"forge-{coupon.id}", "name": coupon.name,
                                     "duration": coupon.duration}  # fmt: skip
            if coupon.percent_off is not None:
                terms["percent_off"] = coupon.percent_off
            else:
                terms |= {"amount_off": coupon.amount_off, "currency": coupon.currency}
            if coupon.duration == "repeating":
                terms["duration_in_months"] = coupon.duration_in_months
            made = await client.post("/v1/coupons", terms,
                                     idempotency_key=f"forge-coupon-{coupon.id}")  # fmt: skip
            code = await client.post("/v1/promotion_codes",
                                     {"coupon": made["id"], "code": coupon.code},
                                     idempotency_key=f"forge-code-{coupon.id}")  # fmt: skip
            await self.save_coupon(coupon.id, str(made["id"]), str(code["id"]))
            report.created.append(f"coupon {coupon.code}")
        elif coupon.archived_at and coupon.stripe_promotion and not coupon.synced_at:
            await client.post(f"/v1/promotion_codes/{coupon.stripe_promotion}", {"active": False},
                              idempotency_key=f"forge-code-off-{coupon.id}")  # fmt: skip
            await self.save_coupon(coupon.id, coupon.stripe_coupon, coupon.stripe_promotion)
            report.archived.append(f"coupon {coupon.code}")
        else:
            report.unchanged += 1

    async def save_coupon(self, coupon_id: str, stripe_coupon: str, promotion: str) -> None:
        async with self.db.session() as session, session.begin():
            row = await session.get(Coupon, coupon_id)
            if row is not None:
                row.stripe_coupon, row.stripe_promotion = stripe_coupon, promotion
                row.synced_at = time.time()

    async def put_item(self, item_id: str, body: ItemIn) -> CatalogItem:
        """Add an item or change it (a change counts as a new version)."""
        values = {"kind": body.kind, "name": body.name, "description": body.description,
                  "currency": body.currency, "amounts": json.dumps(body.amounts, sort_keys=True),
                  "trial_days": body.trial_days,
                  "limits": body.limits.model_dump_json()}  # fmt: skip
        now = time.time()
        async with self.db.session() as session, session.begin():
            row = await session.get(CatalogItem, item_id)
            if row is None:
                row = CatalogItem(id=item_id, created_at=now, updated_at=now, version=1, **values)
                session.add(row)
            elif row.kind != body.kind:
                raise ValueError("an item keeps its kind; add a new one")
            elif any(getattr(row, k) != v for k, v in values.items()) or row.archived_at:
                for name, value in values.items():
                    setattr(row, name, value)
                row.version, row.updated_at, row.archived_at = row.version + 1, now, 0.0
            return row

    async def remove_item(self, item_id: str) -> bool:
        """Archive an item (Stripe archives it at the next sync); False if there is none."""
        async with self.db.session() as session, session.begin():
            row = await session.get(CatalogItem, item_id)
            if row is None:
                return False
            if not row.archived_at:
                now = time.time()
                row.archived_at, row.updated_at, row.version = now, now, row.version + 1
            return True

    async def put_coupon(self, coupon_id: str, body: CouponIn) -> Coupon:
        """Add a coupon, or change one Stripe has not seen yet (PermissionError otherwise)."""
        values = body.model_dump()
        async with self.db.session() as session, session.begin():
            row = await session.get(Coupon, coupon_id)
            if row is not None and row.stripe_coupon:
                if any(getattr(row, k) != v for k, v in values.items()):
                    raise PermissionError("Stripe has this coupon: its terms stay; add a new one")
                return row
            if row is None:
                row = Coupon(id=coupon_id, created_at=time.time(), **values)
                session.add(row)
            else:
                for name, value in values.items():
                    setattr(row, name, value)
            return row

    async def remove_coupon(self, coupon_id: str) -> bool:
        """Retire a coupon: its code stops working at the next sync."""
        async with self.db.session() as session, session.begin():
            row = await session.get(Coupon, coupon_id)
            if row is None:
                return False
            if not row.archived_at:
                row.archived_at, row.synced_at = time.time(), 0.0
            return True

    async def shown(self) -> dict[str, Any]:
        """The catalog as the admin's page shows it."""
        async with self.db.session() as session:
            items = list(await session.scalars(select(CatalogItem).order_by(CatalogItem.id)))
            coupons = list(await session.scalars(select(Coupon).order_by(Coupon.id)))
        return {"items": [item_view(i) for i in items],
                "coupons": [coupon_view(c) for c in coupons]}  # fmt: skip


def item_view(row: CatalogItem) -> dict[str, Any]:
    """One item, with what Stripe has of it."""
    return {"id": row.id, "kind": row.kind, "name": row.name, "description": row.description,
            "currency": row.currency, "amounts": json.loads(row.amounts),
            "trial_days": row.trial_days, "limits": json.loads(row.limits),
            "archived_at": row.archived_at, "stripe_product": row.stripe_product,
            "prices": json.loads(row.stripe_prices or "{}"),
            "synced": bool(row.synced_at) and row.synced_at >= row.updated_at}  # fmt: skip


def coupon_view(row: Coupon) -> dict[str, Any]:
    """One coupon, with what Stripe has of it."""
    return {"id": row.id, "code": row.code, "name": row.name, "percent_off": row.percent_off,
            "amount_off": row.amount_off, "currency": row.currency, "duration": row.duration,
            "duration_in_months": row.duration_in_months, "archived_at": row.archived_at,
            "synced": bool(row.synced_at)}  # fmt: skip
