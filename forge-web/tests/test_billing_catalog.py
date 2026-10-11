"""The catalog (W32): plans, a one-time price per app, hosting per app and month, add-ons and
coupons, set by the admin and synced to Stripe. Syncing twice changes nothing; a new amount is a
new price (Stripe's prices never change) and the old one is archived; a removed item is archived
at Stripe, never deleted; only admins edit the catalog. Stripe is `stripe_standin.py`."""

from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest

from stripe_standin import TEST_KEY, WEBHOOK_SECRET, StripeStandIn
from support import LiveServer, dev_settings, person

CATALOG = "/api/admin/billing/catalog"
PRO: dict[str, Any] = {
    "kind": "plan", "name": "Pro", "description": "For one maker", "currency": "eur",
    "amounts": {"month": 2900, "year": 29000}, "trial_days": 14,
    "limits": {"projects": 10, "ai_budget_month_eur": 50, "ai_budget_per_product_eur": 20,
               "hosted_apps": 3, "custom_domains": 2, "store_publishing": True},
}  # fmt: skip


@pytest.fixture
def stripe() -> StripeStandIn:
    return StripeStandIn()


@pytest.fixture
def server(tmp_path: Path, stripe: StripeStandIn) -> Iterator[LiveServer]:
    settings = dev_settings(tmp_path / "data")
    settings.billing.enabled = True
    with LiveServer(settings) as live:
        live.services.billing.transport = stripe.transport()
        yield live


@pytest.fixture
async def client(server: LiveServer) -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(base_url=server.url, headers=server.headers(), timeout=30) as c:
        keys = {"secret_key": TEST_KEY, "webhook_secret": WEBHOOK_SECRET}
        assert (await c.put("/api/admin/billing/stripe", json=keys)).status_code == 200
        yield c


async def sync(client: httpx.AsyncClient) -> dict[str, Any]:
    done = await client.post(f"{CATALOG}/sync")
    assert done.status_code == 200, done.text
    return dict(done.json())


async def test_syncing_twice_changes_nothing(
    client: httpx.AsyncClient, stripe: StripeStandIn
) -> None:
    for item_id, item in {
        "pro": PRO,
        "app": {"kind": "app_once", "name": "Your app, once", "amounts": {"once": 49000}},
        "hosting": {"kind": "hosting", "name": "Hosting per app", "amounts": {"month": 900}},
        "mac": {"kind": "addon", "name": "100 Mac minutes", "amounts": {"month": 1500}},
    }.items():  # fmt: skip
        assert (await client.put(f"{CATALOG}/items/{item_id}", json=item)).status_code == 200
    coupon = {"code": "LAUNCH25", "name": "Launch", "percent_off": 25, "duration": "repeating",
              "duration_in_months": 3}  # fmt: skip
    assert (await client.put(f"{CATALOG}/coupons/launch", json=coupon)).status_code == 200
    first = await sync(client)
    assert len(stripe.of("prod")) == 4 and len(stripe.of("price")) == 5  # pro has two
    yearly = next(p for p in stripe.of("price") if p.get("recurring[interval]") == "year")
    assert yearly["unit_amount"] == "29000" and yearly["currency"] == "eur"
    once = next(p for p in stripe.of("price") if p["unit_amount"] == "49000")
    assert "recurring[interval]" not in once  # the app's price is paid once
    [made] = stripe.of("coupon")
    assert made["percent_off"] == "25" and made["duration_in_months"] == "3"
    assert stripe.of("promo")[0]["code"] == "LAUNCH25"
    assert first["unchanged"] == 0 and len(first["created"]) == 10  # 4 products, 5 prices
    effects = len(stripe.effects)
    again = await sync(client)
    assert len(stripe.effects) == effects and again["created"] == [] and again["unchanged"] == 5
    shown = (await client.get(CATALOG)).json()
    pro = next(i for i in shown["items"] if i["id"] == "pro")
    assert pro["limits"]["hosted_apps"] == 3 and pro["synced"] and pro["trial_days"] == 14


async def test_a_new_amount_is_a_new_price_and_a_removed_plan_is_archived(
    client: httpx.AsyncClient, stripe: StripeStandIn
) -> None:
    await client.put(f"{CATALOG}/items/pro", json=PRO)
    await sync(client)
    [old_month] = [p for p in stripe.of("price") if p.get("recurring[interval]") == "month"]
    dearer = PRO | {"amounts": {"month": 3900, "year": 29000}, "name": "Pro+"}
    assert (await client.put(f"{CATALOG}/items/pro", json=dearer)).status_code == 200
    report = await sync(client)
    assert old_month["active"] is False  # archived, still there for running subscriptions
    months = [p for p in stripe.of("price") if p.get("recurring[interval]") == "month"]
    assert [p["unit_amount"] for p in months] == ["2900", "3900"] and months[1]["active"]
    assert stripe.of("prod")[0]["name"] == "Pro+" and "price pro/month" in report["created"]
    assert len([p for p in stripe.of("price") if p.get("recurring[interval]") == "year"]) == 1
    assert (await client.delete(f"{CATALOG}/items/pro")).status_code == 200
    removed = await sync(client)
    assert stripe.of("prod")[0]["active"] is False and not any(
        p["active"] for p in stripe.of("price"))  # fmt: skip
    assert "product pro" in removed["archived"]
    assert not [e for e in stripe.effects if e[0] == "DELETE"]  # nothing is deleted at Stripe
    assert (await sync(client))["archived"] == []
    gone = (await client.get(CATALOG)).json()["items"]
    assert gone[0]["archived_at"] > 0


async def test_a_removed_coupon_code_stops_working(
    client: httpx.AsyncClient, stripe: StripeStandIn
) -> None:
    coupon = {"code": "TENOFF", "name": "Ten off", "amount_off": 1000, "currency": "eur"}
    await client.put(f"{CATALOG}/coupons/ten", json=coupon)
    await sync(client)
    changed = await client.put(f"{CATALOG}/coupons/ten", json=coupon | {"amount_off": 2000})
    assert changed.status_code == 409  # a synced coupon's terms stay; make a new one
    assert (await client.delete(f"{CATALOG}/coupons/ten")).status_code == 200
    await sync(client)
    assert stripe.of("promo")[0]["active"] is False and stripe.of("coupon")[0]["active"]


async def test_the_catalog_is_checked_and_only_admins_edit_it(
    server: LiveServer, client: httpx.AsyncClient
) -> None:
    wrong = [
        PRO | {"amounts": {"once": 100}},  # a plan is paid monthly or yearly
        PRO | {"amounts": {}},
        PRO | {"amounts": {"month": -5}},
        {"kind": "hosting", "name": "x", "amounts": {"month": 900, "year": 9000}},
    ]
    for item in wrong:
        assert (await client.put(f"{CATALOG}/items/pro", json=item)).status_code == 422, item
    both = {"code": "BOTH", "name": "x", "percent_off": 10, "amount_off": 100}
    assert (await client.put(f"{CATALOG}/coupons/both", json=both)).status_code == 422
    maria = await person(server, "maria")
    assert (await maria.web.get(CATALOG)).status_code == 403
    assert (await maria.web.request("PUT", f"{CATALOG}/items/pro", json=PRO)).status_code == 403
    await maria.web.client.aclose()


async def test_without_keys_there_is_nothing_to_sync(tmp_path: Path) -> None:
    settings = dev_settings(tmp_path / "data")
    settings.billing.enabled = True
    with LiveServer(settings) as live:
        async with httpx.AsyncClient(base_url=live.url, headers=live.headers()) as c:
            assert (await c.post(f"{CATALOG}/sync")).status_code == 409
