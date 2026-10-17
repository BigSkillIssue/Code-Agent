"""Entitlements (W33a): one engine sets every limit of a user, wherever it is enforced.

Admins keep the server's own limits (any number of projects). With billing off, the server's own
settings apply as before: projects per user, disk per project, the monthly budget on the server's
AI keys (with its grants), Mac minutes. With billing on, a customer without a subscription (unless
the admin named a free plan) gets those too, but no hosted app, no own domain and no store
publishing: those are paid for. A customer with a subscription gets the limits of their plan, plus
what their add-ons add; hosting bought per app adds hosted apps. Plans set AI budgets in EUR; the
gateway counts USD. A plan without an AI budget leaves the server's AI keys to the server's own
policy (its grants and default budget), never unlimited: they are the admin's money.
"""

import json
from dataclasses import dataclass, fields, replace

from sqlalchemy import select

from forge_web.billing.catalog import Limits
from forge_web.db.billing_models import CatalogItem, Subscription
from forge_web.db.engine import Database
from forge_web.db.models import Project, User
from forge_web.gateway.keys import server_key_limit
from forge_web.settings import WebSettings

PAID = ("active", "trialing", "past_due")  # past due: dunning (W33b) decides what follows


@dataclass(frozen=True)
class Entitlements:
    """Every limit of one user (None: no limit), and where they come from."""

    source: str  # "admin", "server" (the server's settings) or "plan:<catalog item>"
    projects: int | None = None
    ai_month_usd: float | None = None  # on the server's AI keys
    ai_product_usd: float | None = None  # the same, for each product (project) on its own
    server_keys: bool = True  # may use the server's AI keys at all
    mac_minutes: float | None = None  # a month
    hosted_apps: int | None = None
    storage_mb: int | None = None  # per project
    custom_domains: int | None = None
    store_publishing: bool = True


class EntitlementEngine:
    """Answers what a user (or a project's owner) may use."""

    def __init__(self, db: Database, settings: WebSettings) -> None:
        self.db, self.settings = db, settings

    async def of(self, user: User) -> Entitlements:
        """The user's limits."""
        if user.role == "admin":  # the server's limits, but no count of projects
            return replace(await self.server_defaults(user), source="admin", projects=None)
        if self.settings.billing.enabled:
            paid = await self.plan_of(user)
            if paid is not None:
                return paid
            unpaid = await self.server_defaults(user)
            return replace(unpaid, hosted_apps=0, custom_domains=0, store_publishing=False)
        return await self.server_defaults(user)

    async def of_project(self, project_id: str) -> Entitlements:
        """The limits of a project's owner."""
        async with self.db.session() as session:
            project = await session.get(Project, project_id)
            owner = await session.get(User, project.owner_id) if project else None
        if owner is None:
            return await self.server_defaults(None)
        return await self.of(owner)

    async def server_defaults(self, user: User | None) -> Entitlements:
        """The server's own limits."""
        settings = self.settings
        limit = await server_key_limit(self.db, user, settings.gateway) if user else None
        return Entitlements(
            source="server",
            projects=settings.quotas.projects_per_user or None,
            ai_month_usd=limit, server_keys=limit is not None,
            mac_minutes=settings.apple.minutes_per_month or None,
            storage_mb=settings.quotas.project_disk_mb or None,
        )  # fmt: skip

    async def plan_of(self, user: User) -> Entitlements | None:
        """The limits of the user's plan and add-ons (None without a plan)."""
        async with self.db.session() as session:
            paid = list(await session.scalars(
                select(Subscription).where(Subscription.user_id == user.id,
                                           Subscription.status.in_(PAID))
                .order_by(Subscription.created_at.desc())))  # fmt: skip
            wanted = {s.item_id for s in paid} | {self.settings.billing.free_plan}
            items = {i.id: i for i in await session.scalars(
                select(CatalogItem).where(CatalogItem.id.in_(wanted)))}  # fmt: skip
        plans = [items[s.item_id] for s in paid if s.item_id in items
                 and items[s.item_id].kind == "plan"]  # fmt: skip
        free = items.get(self.settings.billing.free_plan)
        plan = plans[0] if plans else free if free is not None and free.kind == "plan" else None
        if plan is None:
            return None
        found = self.from_limits(f"plan:{plan.id}", Limits.model_validate(json.loads(plan.limits)))
        if found.ai_month_usd is None:  # no budget in the plan: the server's own policy
            policy = await self.server_defaults(user)
            found = replace(found, ai_month_usd=policy.ai_month_usd,
                            server_keys=policy.server_keys)  # fmt: skip
        for row in paid:
            item = items.get(row.item_id)
            if item is not None and item.kind in ("addon", "hosting"):
                found = added(found, item, row.quantity)
        return found

    def from_limits(self, source: str, limits: Limits) -> Entitlements:
        """A plan's limits as entitlements (EUR budgets in USD)."""
        rate = self.settings.billing.usd_per_eur
        return Entitlements(
            source=source, projects=limits.projects,
            ai_month_usd=None if limits.ai_budget_month_eur is None
            else limits.ai_budget_month_eur * rate,
            ai_product_usd=None if limits.ai_budget_per_product_eur is None
            else limits.ai_budget_per_product_eur * rate,
            mac_minutes=limits.mac_minutes, hosted_apps=limits.hosted_apps,
            storage_mb=None if limits.storage_gb is None else int(limits.storage_gb * 1000),
            custom_domains=limits.custom_domains, store_publishing=limits.store_publishing,
        )  # fmt: skip


def added(base: Entitlements, item: CatalogItem, quantity: int) -> Entitlements:
    """What an add-on (or hosting for some apps) adds to a plan's limits."""
    if item.kind == "hosting":
        more: dict[str, float] = {"hosted_apps": quantity}
    else:
        extra = Limits.model_validate(json.loads(item.limits))
        more = {"projects": extra.projects or 0, "mac_minutes": extra.mac_minutes or 0,
                "hosted_apps": extra.hosted_apps or 0, "custom_domains": extra.custom_domains or 0,
                "storage_mb": int((extra.storage_gb or 0) * 1000)}  # fmt: skip
        more = {name: value * quantity for name, value in more.items()}
    changes = {f.name: getattr(base, f.name) + more[f.name] for f in fields(base)
               if more.get(f.name) and getattr(base, f.name) is not None}  # fmt: skip
    return replace(base, **changes)
