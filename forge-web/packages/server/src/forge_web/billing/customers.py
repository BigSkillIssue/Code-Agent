"""The admin's view and control of every customer's billing (W40c).

`/api/admin/billing/customers` lists every account with what it pays for, its free plan and its
dunning; one customer's page shows their limits, subscriptions, orders, apps, dunning case and
cancellations. The admin gives a plan for free (for good or until a date) and takes it back, ends
a subscription at Stripe at once or at its period's end, holds, resumes or closes a dunning case,
unlocks an app or takes it back, and carries out a cancellation that waits for an admin
(`/api/admin/billing/cancellations`). Every action goes into the audit log.
"""

import time
from dataclasses import asdict
from typing import Annotated, Any, Literal

from fastapi import APIRouter, HTTPException, Path, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, or_, select

from forge_web.audit import audit
from forge_web.auth.sessions import AdminUser, client_ip
from forge_web.billing.cancel import confirm, end_at_period_end
from forge_web.billing.checkout import subscription_view
from forge_web.billing.entitlements import PAID
from forge_web.billing.licenses import grant, revoke
from forge_web.billing.stripe_client import StripeError
from forge_web.db.billing_models import (
    AppLicense,
    BillingCustomer,
    Cancellation,
    CatalogItem,
    Checkout,
    DunningCase,
    PlanGift,
    Subscription,
)
from forge_web.db.models import Project, User
from forge_web.services import Services, services_of

ID = r"^[A-Za-z0-9_-]{1,64}$"
SHOWN = 200  # customers in one list
Id = Annotated[str, Path(pattern=ID)]


class GiftIn(BaseModel):
    """A plan the admin gives for free."""

    model_config = ConfigDict(extra="forbid")
    item_id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{0,39}$")
    until: float = Field(default=0.0, ge=0)  # 0: for good
    note: str = Field(default="", max_length=500)


class EndIn(BaseModel):
    """How a subscription ends: at once, or when its paid period ends."""

    model_config = ConfigDict(extra="forbid")
    now: bool = False


class CarryOutIn(BaseModel):
    """How a cancellation that waits for an admin is carried out."""

    model_config = ConfigDict(extra="forbid")
    action: Literal["period_end", "now", "nothing"]  # nothing: no contract left to end


def customers_router() -> APIRouter:
    """`/api/admin/billing/customers` and `/api/admin/billing/cancellations`."""
    router = APIRouter(prefix="/api/admin/billing")

    @router.get("/customers")
    async def listed(request: Request, admin: AdminUser, q: str = "") -> list[dict[str, Any]]:
        return await customer_list(services_of(request), q[:200])

    @router.get("/customers/{user_id}")
    async def shown(user_id: Id, request: Request, admin: AdminUser) -> dict[str, Any]:
        return await customer_view(services_of(request), user_id)

    gift_routes(router)
    action_routes(router)
    cancellation_routes(router)
    return router


async def customer_of(services: Services, user_id: str) -> User:
    """The account (404 when there is none)."""
    async with services.db.session() as session:
        user = await session.get(User, user_id)
    if user is None:
        raise HTTPException(404, "no such account")
    return user


async def noted(services: Services, request: Request, admin: User, action: str, target: str,
                **detail: Any) -> None:  # fmt: skip
    """The admin's action in the audit log."""
    await audit(services.db, f"billing.admin_{action}", user_id=admin.id, target=target,
                ip=client_ip(request), **detail)  # fmt: skip


async def customer_list(services: Services, q: str) -> list[dict[str, Any]]:
    """Every account (or those whose name or address has `q`) with its billing in brief."""
    async with services.db.session() as session:
        query = select(User).order_by(User.email).limit(SHOWN)
        if q:
            like = f"%{q.lower()}%"
            query = query.where(or_(func.lower(User.email).like(like),
                                    func.lower(User.name).like(like)))  # fmt: skip
        users = list(await session.scalars(query))
        ids = [u.id for u in users]
        paying = list(await session.scalars(select(Subscription).where(
            Subscription.user_id.in_(ids), Subscription.status.in_(PAID))))  # fmt: skip
        cases = {c.user_id: c for c in await session.scalars(select(DunningCase).where(
            DunningCase.user_id.in_(ids), DunningCase.closed_at == 0))}  # fmt: skip
        gifts = {g.user_id: g for g in await session.scalars(select(PlanGift).where(
            PlanGift.user_id.in_(ids)))}  # fmt: skip
    return [{"id": u.id, "email": u.email, "name": u.name, "role": u.role, "status": u.status,
             "paying": sorted({s.item_id for s in paying if s.user_id == u.id}),
             "gift": gifts[u.id].item_id if u.id in gifts else "",
             "dunning": cases[u.id].stage if u.id in cases else "",
             "held": bool(u.id in cases and cases[u.id].held_at)} for u in users]  # fmt: skip


async def customer_view(services: Services, user_id: str) -> dict[str, Any]:
    """Everything about one customer's billing."""
    user = await customer_of(services, user_id)
    limits = await services.entitlements.of(user)
    async with services.db.session() as session:
        stripe = await session.get(BillingCustomer, user_id)
        subs = list(await session.scalars(select(Subscription).where(
            Subscription.user_id == user_id).order_by(Subscription.created_at.desc())))  # fmt: skip
        orders = list(await session.scalars(
            select(Checkout).where(Checkout.user_id == user_id)
            .order_by(Checkout.created_at.desc()).limit(50)))  # fmt: skip
        case = await session.scalar(select(DunningCase).where(DunningCase.user_id == user_id)
                                    .order_by(DunningCase.opened_at.desc()).limit(1))  # fmt: skip
        gift = await session.get(PlanGift, user_id)
        cancellations = list(await session.scalars(select(Cancellation).where(
            Cancellation.user_id == user_id).order_by(Cancellation.received_at.desc())
            .limit(20)))  # fmt: skip
    return {
        "id": user.id, "email": user.email, "name": user.name, "role": user.role,
        "stripe_customer": stripe.stripe_customer if stripe else "",
        "entitlements": asdict(limits),
        "subscriptions": [{**subscription_view(s), "stripe_subscription": s.stripe_subscription}
                          for s in subs],
        "orders": [order_view(o) for o in orders],
        "apps": await apps_view(services, user_id),
        "dunning": dunning_view(case),
        "gift": gift_view(gift),
        "cancellations": [cancellation_view(c) for c in cancellations],
    }  # fmt: skip


def order_view(row: Checkout) -> dict[str, Any]:
    return {"id": row.id, "item_id": row.item_id, "interval": row.interval, "status": row.status,
            "created_at": row.created_at, "completed_at": row.completed_at,
            "project_id": row.project_id, "trial_days": row.trial_days}  # fmt: skip


def dunning_view(case: DunningCase | None) -> dict[str, Any] | None:
    if case is None:
        return None
    return {"id": case.id, "stage": case.stage, "opened_at": case.opened_at,
            "stage_at": case.stage_at, "closed_at": case.closed_at, "held_at": case.held_at,
            "invoices": case.invoices}  # fmt: skip


def gift_view(gift: PlanGift | None) -> dict[str, Any] | None:
    if gift is None:
        return None
    return {"item_id": gift.item_id, "until": gift.until, "note": gift.note,
            "granted_by": gift.granted_by, "granted_at": gift.granted_at}  # fmt: skip


def cancellation_view(row: Cancellation) -> dict[str, Any]:
    return {"id": row.id, "received_at": row.received_at, "name": row.name, "email": row.email,
            "contract": row.contract, "extraordinary": row.extraordinary, "reason": row.reason,
            "status": row.status, "effective_at": row.effective_at,
            "user_id": row.user_id}  # fmt: skip


async def apps_view(services: Services, user_id: str) -> list[dict[str, Any]]:
    """The customer's own app projects and how each was unlocked."""
    async with services.db.session() as session:
        projects = list(await session.scalars(
            select(Project).where(Project.owner_id == user_id, Project.kind == "app")
            .order_by(Project.name)))  # fmt: skip
        licenses = list(await session.scalars(select(AppLicense).where(
            AppLicense.project_id.in_([p.id for p in projects]))
            .order_by(AppLicense.granted_at)))  # fmt: skip
    shown = []
    for project in projects:
        own = [x for x in licenses if x.project_id == project.id]
        live = next((x for x in own if not x.revoked_at), None)
        shown.append({"project_id": project.id, "name": project.name, "unlocked": live is not None,
                      "by": ("admin" if live.granted_by else "paid") if live else "",
                      "history": [{"granted_at": x.granted_at, "granted_by": x.granted_by,
                                   "checkout_id": x.checkout_id, "revoked_at": x.revoked_at,
                                   "revoked_reason": x.revoked_reason} for x in own]})  # fmt: skip
    return shown


def gift_routes(router: APIRouter) -> None:
    """A plan for free, and taking it back."""

    @router.put("/customers/{user_id}/gift")
    async def give(user_id: Id, body: GiftIn, request: Request,
                   admin: AdminUser) -> dict[str, Any]:  # fmt: skip
        services = services_of(request)
        await customer_of(services, user_id)
        async with services.db.session() as session, session.begin():
            item = await session.get(CatalogItem, body.item_id)
            if item is None or item.kind != "plan" or item.archived_at:
                raise HTTPException(409, "choose a plan of the catalog")
            if body.until and body.until <= time.time():
                raise HTTPException(422, "the date lies in the past")
            gift = await session.get(PlanGift, user_id)
            if gift is None:
                gift = PlanGift(user_id=user_id, granted_at=time.time())
                session.add(gift)
            gift.item_id, gift.until, gift.note = body.item_id, body.until, body.note
            gift.granted_by, gift.granted_at = admin.id, time.time()
        await noted(services, request, admin, "gift", user_id, item=body.item_id, until=body.until)
        return gift_view(gift) or {}

    @router.delete("/customers/{user_id}/gift")
    async def take_back(user_id: Id, request: Request, admin: AdminUser) -> dict[str, bool]:
        services = services_of(request)
        async with services.db.session() as session, session.begin():
            gift = await session.get(PlanGift, user_id)
            if gift is None:
                raise HTTPException(404, "no plan was given")
            await session.delete(gift)
        await noted(services, request, admin, "gift_removed", user_id)
        return {"removed": True}


def action_routes(router: APIRouter) -> None:
    """Subscriptions, dunning and apps of one customer."""

    @router.post("/customers/{user_id}/subscriptions/{subscription_id}/end")
    async def end(user_id: Id, subscription_id: Id, body: EndIn, request: Request,
                  admin: AdminUser) -> dict[str, bool]:  # fmt: skip
        services = services_of(request)
        await end_subscription(services, user_id, subscription_id, body.now)
        await noted(services, request, admin, "subscription_ended", user_id,
                    subscription=subscription_id, now=body.now)  # fmt: skip
        return {"ended": True}

    @router.post("/customers/{user_id}/dunning/{action}")
    async def dunning(user_id: Id, action: Literal["hold", "resume", "close"], request: Request,
                      admin: AdminUser) -> dict[str, bool]:  # fmt: skip
        services, now = services_of(request), time.time()
        if action == "close":
            changed = await services.dunning.close_case(user_id, now)
        else:
            changed = await services.dunning.hold(user_id, now, action == "hold")
        if not changed:
            raise HTTPException(404, "there is no open dunning case")
        await noted(services, request, admin, f"dunning_{action}", user_id)
        return {"changed": True}

    @router.post("/customers/{user_id}/apps/{project_id}/{change}")
    async def app(user_id: Id, project_id: Id, change: Literal["unlock", "lock"],
                  request: Request, admin: AdminUser) -> dict[str, bool]:  # fmt: skip
        services = services_of(request)
        async with services.db.session() as session:
            project = await session.get(Project, project_id)
        if project is None or project.owner_id != user_id or project.kind != "app":
            raise HTTPException(404, "no such app of this customer")
        if change == "unlock":
            await grant(services.db, project_id, "", by=admin.id)
        elif not await revoke(services.db, "taken back by an admin", project_id=project_id):
            raise HTTPException(404, "the app is not unlocked")
        await noted(services, request, admin, f"app_{change}", user_id, project=project_id)
        return {"changed": True}


async def end_subscription(services: Services, user_id: str, stripe_id: str, now: bool) -> None:
    """End one of the customer's subscriptions at Stripe, at once or at its period's end."""
    async with services.db.session() as session:
        rows = list(
            await session.scalars(
                select(Subscription).where(
                    Subscription.user_id == user_id, Subscription.stripe_subscription == stripe_id
                )
            )
        )
    if not rows:
        raise HTTPException(404, "no such subscription of this customer")
    client = await services.billing.client()
    if client is None:
        raise HTTPException(409, "set the Stripe keys first")
    try:
        if now:
            await client.call("DELETE", f"/v1/subscriptions/{stripe_id}")
        else:
            await client.post(f"/v1/subscriptions/{stripe_id}", {"cancel_at_period_end": True})
    except StripeError as error:
        raise HTTPException(502, f"Stripe: {error}") from None
    finally:
        await client.close()
    if now:  # Stripe's event follows; until then the limits already end here
        async with services.db.session() as session, session.begin():
            for row in await session.scalars(select(Subscription).where(
                    Subscription.stripe_subscription == stripe_id)):  # fmt: skip
                row.status, row.updated_at = "canceled", time.time()


def cancellation_routes(router: APIRouter) -> None:
    """The cancellations, and carrying out one that waits for an admin."""

    @router.get("/cancellations")
    async def cancellations(request: Request, admin: AdminUser) -> dict[str, Any]:
        services = services_of(request)
        async with services.db.session() as session:
            rows = list(await session.scalars(select(Cancellation).order_by(
                Cancellation.received_at.desc()).limit(SHOWN)))  # fmt: skip
        return {
            "waiting": [cancellation_view(r) for r in rows if r.status == "manual"],
            "recent": [cancellation_view(r) for r in rows if r.status != "manual"],
        }

    @router.post("/cancellations/{cancellation_id}")
    async def carry_out(cancellation_id: Id, body: CarryOutIn, request: Request,
                        admin: AdminUser) -> dict[str, Any]:  # fmt: skip
        services = services_of(request)
        row = await carried_out(services, cancellation_id, body.action)
        await noted(services, request, admin, "cancellation_done", row.user_id or row.email,
                    cancellation=row.id, how=body.action)  # fmt: skip
        services.background(confirm(services, row))  # when it takes effect, by mail
        return cancellation_view(row)


async def carried_out(services: Services, cancellation_id: str, action: str) -> Cancellation:
    """End what a waiting cancellation names, and record when it takes effect."""
    async with services.db.session() as session:
        row = await session.get(Cancellation, cancellation_id)
        user = await session.get(User, row.user_id) if row and row.user_id else None
    if row is None:
        raise HTTPException(404, "no such cancellation")
    if row.status != "manual":
        raise HTTPException(409, "this cancellation is carried out already")
    effective = time.time()
    if action != "nothing":
        if user is None:
            raise HTTPException(409, "no account matches; there is nothing to end here")
        effective = await ended(services, user, row, action == "now")
    async with services.db.session() as session, session.begin():
        stored = await session.get(Cancellation, cancellation_id)
        if stored is None or stored.status != "manual":
            raise HTTPException(409, "this cancellation is carried out already")
        stored.status, stored.effective_at = "done", effective
    row.status, row.effective_at = "done", effective
    return row


async def ended(services: Services, user: User, row: Cancellation, now: bool) -> float:
    """End the user's subscriptions the cancellation names; when that takes effect."""
    if not now:
        effective, _ = await end_at_period_end(services, user, row.contract, row.id)
        if not effective:
            raise HTTPException(409, "nothing to end, or Stripe is not reachable")
        return effective
    async with services.db.session() as session:
        rows = list(await session.scalars(select(Subscription).where(
            Subscription.user_id == user.id, Subscription.status.in_(PAID))))  # fmt: skip
    named = {
        r.stripe_subscription
        for r in rows
        if r.stripe_subscription
        and (not row.contract or row.contract in (r.item_id, r.stripe_subscription))
    }
    if not named:
        raise HTTPException(409, "nothing to end")
    for stripe_id in sorted(named):
        await end_subscription(services, user.id, stripe_id, True)
    return time.time()
