"""The admin's side of billing: `/api/admin/billing/stripe`, the Stripe keys (W31), and
`/api/admin/billing/catalog`, what customers can buy, synced to Stripe (W32)."""

import dataclasses
from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Path, Request

from forge_web.audit import audit
from forge_web.auth.sessions import AdminUser, client_ip
from forge_web.billing.catalog import CouponIn, ItemIn, coupon_view, item_view
from forge_web.billing.setup import StripeKeysIn
from forge_web.billing.stripe_client import StripeError
from forge_web.services import Services, services_of


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

    catalog_routes(router)
    return router


SLUG = r"^[a-z0-9][a-z0-9-]{0,39}$"  # the admin's names for items and coupons


def catalog_routes(router: APIRouter) -> None:
    """The catalog: items, coupons, and the sync to Stripe."""

    @router.get("/catalog")
    async def catalog(request: Request, admin: AdminUser) -> dict[str, Any]:
        return await services_of(request).billing.catalog.shown()

    @router.put("/catalog/items/{item_id}")
    async def put_item(
        item_id: Annotated[str, Path(pattern=SLUG)],
        body: ItemIn,
        request: Request,
        admin: AdminUser,
    ) -> dict[str, Any]:
        services = services_of(request)
        try:
            row = await services.billing.catalog.put_item(item_id, body)
        except ValueError as error:
            raise HTTPException(409, str(error)) from None
        await changed(services, request, admin.id, "billing.item_set", item_id)
        return item_view(row)

    @router.delete("/catalog/items/{item_id}")
    async def remove_item(item_id: str, request: Request, admin: AdminUser) -> dict[str, bool]:
        services = services_of(request)
        if not await services.billing.catalog.remove_item(item_id):
            raise HTTPException(404, "no such item")
        await changed(services, request, admin.id, "billing.item_removed", item_id)
        return {"removed": True}

    @router.put("/catalog/coupons/{coupon_id}")
    async def put_coupon(
        coupon_id: Annotated[str, Path(pattern=SLUG)],
        body: CouponIn,
        request: Request,
        admin: AdminUser,
    ) -> dict[str, Any]:
        services = services_of(request)
        try:
            row = await services.billing.catalog.put_coupon(coupon_id, body)
        except PermissionError as error:
            raise HTTPException(409, str(error)) from None
        await changed(services, request, admin.id, "billing.coupon_set", coupon_id)
        return coupon_view(row)

    @router.delete("/catalog/coupons/{coupon_id}")
    async def remove_coupon(coupon_id: str, request: Request, admin: AdminUser) -> dict[str, bool]:
        services = services_of(request)
        if not await services.billing.catalog.remove_coupon(coupon_id):
            raise HTTPException(404, "no such coupon")
        await changed(services, request, admin.id, "billing.coupon_removed", coupon_id)
        return {"removed": True}

    @router.post("/catalog/sync")
    async def sync(request: Request, admin: AdminUser) -> dict[str, Any]:
        services = services_of(request)
        client = await services.billing.client()
        if client is None:
            raise HTTPException(409, "set the Stripe keys first")
        try:
            report = await services.billing.catalog.sync(client)
        except StripeError as error:
            raise HTTPException(502, f"Stripe: {error}") from None
        finally:
            await client.close()
        await changed(services, request, admin.id, "billing.catalog_synced", "")
        return dataclasses.asdict(report)


async def changed(
    services: Services, request: Request, user_id: str, action: str, what: str
) -> None:
    """Note a change of the catalog in the audit log."""
    await audit(services.db, action, user_id=user_id, target=what, ip=client_ip(request))
