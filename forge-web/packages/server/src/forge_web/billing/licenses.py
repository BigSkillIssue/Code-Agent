"""Apps unlocked by their one-time price (W40b).

While the admin sells a one-time price per app (an `app_once` item that Stripe has a price for),
a customer's app goes live in production only once it is paid for: the order names one of the
customer's own app projects, and only a paid order unlocks it (a delayed payment such as SEPA once
it arrived). A full refund takes it back. Staging stays free, so an app can be tried first; an app
that already runs keeps running. Admins' own apps need nothing, and an admin can unlock an app for
free or take it back (W40c).
"""

import json
import secrets
import time

from fastapi import HTTPException
from sqlalchemy import select

from forge_web.db.billing_models import AppLicense, CatalogItem
from forge_web.db.engine import Database
from forge_web.db.models import Project, User
from forge_web.services import Services


async def per_app_price(db: Database) -> bool:
    """Whether the catalog sells a one-time price per app now."""
    async with db.session() as session:
        items = list(await session.scalars(select(CatalogItem).where(
            CatalogItem.kind == "app_once", CatalogItem.archived_at == 0)))  # fmt: skip
    return any(json.loads(item.stripe_prices or "{}").get("once") for item in items)


async def unlocked(db: Database, project_id: str) -> AppLicense | None:
    """The license that unlocks the app, if there is one."""
    async with db.session() as session:
        return await session.scalar(select(AppLicense).where(
            AppLicense.project_id == project_id, AppLicense.revoked_at == 0).limit(1))  # fmt: skip


async def grant(db: Database, project_id: str, user_id: str, checkout_id: str = "",
                by: str = "") -> AppLicense:  # fmt: skip
    """Unlock the app (once for each order; an admin's gift has `by`)."""
    async with db.session() as session, session.begin():
        if checkout_id:
            found = await session.scalar(select(AppLicense).where(
                AppLicense.checkout_id == checkout_id))  # fmt: skip
            if found is not None:
                return found
        row = AppLicense(id=secrets.token_hex(8), project_id=project_id, user_id=user_id,
                         checkout_id=checkout_id, granted_at=time.time(),
                         granted_by=by)  # fmt: skip
        session.add(row)
    return row


async def revoke(db: Database, reason: str, checkout_id: str = "", project_id: str = "") -> int:
    """Take back what an order (or, by project, every license of an app) unlocked; how many."""
    if not checkout_id and not project_id:
        return 0
    taken = 0
    async with db.session() as session, session.begin():
        query = select(AppLicense).where(AppLicense.revoked_at == 0)
        query = query.where(AppLicense.checkout_id == checkout_id) if checkout_id else query.where(
            AppLicense.project_id == project_id)  # fmt: skip
        for row in await session.scalars(query):
            row.revoked_at, row.revoked_reason = time.time(), reason[:200]
            taken += 1
    return taken


async def own_app(db: Database, user: User, project_id: str) -> Project:
    """The user's own app project an order unlocks (404 or 409 otherwise)."""
    async with db.session() as session:
        project = await session.get(Project, project_id) if project_id else None
    if project is None or project.owner_id != user.id:
        raise HTTPException(404, "choose one of your own apps")
    if project.kind != "app":
        raise HTTPException(409, "only app projects have a one-time price")
    if await unlocked(db, project_id) is not None:
        raise HTTPException(409, "this app is paid for already")
    return project


async def require_paid(services: Services, project: Project) -> None:
    """403 while the admin sells a one-time price per app and this app is not paid for."""
    if not services.settings.billing.enabled:
        return
    async with services.db.session() as session:
        owner = await session.get(User, project.owner_id)
    if owner is None or owner.role == "admin" or not await per_app_price(services.db):
        return
    if await unlocked(services.db, project.id) is None:
        raise HTTPException(403, "pay this app's one-time price on the billing page first")
