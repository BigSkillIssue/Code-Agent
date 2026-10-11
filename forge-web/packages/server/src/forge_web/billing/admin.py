"""The admin's side of billing (W31): `/api/admin/billing/stripe`, the Stripe keys."""

from typing import Any

from fastapi import APIRouter, HTTPException, Request

from forge_web.audit import audit
from forge_web.auth.sessions import AdminUser, client_ip
from forge_web.billing.setup import StripeKeysIn
from forge_web.services import services_of


def billing_admin_router() -> APIRouter:
    """`/api/admin/billing/stripe`: the admin's Stripe keys."""
    router = APIRouter(prefix="/api/admin/billing")

    @router.get("/stripe")
    async def shown(request: Request, admin: AdminUser) -> dict[str, Any]:
        return await services_of(request).billing.view()

    @router.put("/stripe")
    async def save(body: StripeKeysIn, request: Request, admin: AdminUser) -> dict[str, Any]:
        services = services_of(request)
        try:
            await services.billing.save(body, admin.id)
        except ValueError as error:
            raise HTTPException(422, str(error)) from None
        except PermissionError as error:
            raise HTTPException(409, str(error)) from None
        view = await services.billing.view()
        await audit(services.db, "billing.keys_set", user_id=admin.id, ip=client_ip(request),
                    mode=view["mode"])  # fmt: skip
        return view

    return router
