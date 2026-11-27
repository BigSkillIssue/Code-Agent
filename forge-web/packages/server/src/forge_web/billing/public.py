"""What visitors see before they sign in (W72): the plans with their prices and the trial, for the
landing page. Read-only and without a session; nothing of Stripe's (ids, products) is shown."""

import json
from typing import Any

from fastapi import APIRouter, Request
from sqlalchemy import select

from forge_web.db.billing_models import CatalogItem
from forge_web.services import services_of


def public_view(row: CatalogItem) -> dict[str, Any]:
    """A plan as a visitor sees it."""
    return {"id": row.id, "kind": row.kind, "name": row.name, "description": row.description,
            "currency": row.currency, "amounts": json.loads(row.amounts),
            "trial_days": row.trial_days, "limits": json.loads(row.limits)}  # fmt: skip


def public_router() -> APIRouter:
    """`GET /api/public/plans`: the plans customers can order now, and the trial."""
    router = APIRouter(prefix="/api/public")

    @router.get("/plans")
    async def plans(request: Request) -> dict[str, Any]:
        services = services_of(request)
        billing = services.settings.billing
        if not billing.enabled:
            return {"plans": [], "trial": None}
        async with services.db.session() as session:
            rows = list(await session.scalars(select(CatalogItem).where(
                CatalogItem.archived_at == 0, CatalogItem.kind == "plan")
                .order_by(CatalogItem.id)))  # fmt: skip
        shown = [public_view(row) for row in rows if json.loads(row.stripe_prices or "{}")]
        trial = ({"days": billing.trial_days, "credit_usd": billing.trial_credit_usd}
                 if billing.trial_days > 0 else None)  # fmt: skip
        return {"plans": shown, "trial": trial}

    return router
