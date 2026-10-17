"""Checkout (W33a2): customers order from the catalog through Stripe Checkout (no card data on
this server, Stripe Tax on); the order needs the waiver of the right of withdrawal, which the
confirmation mail repeats; a paid checkout grants its entitlements through Stripe's events, a
cancelled or expired one grants nothing, and an ended subscription takes them away again. A trial
comes once and with plans only; a second plan is refused, and a new order of a plan ends the
older open one (W40a)."""

import asyncio
import time
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest

from forge_web.billing import checkout
from forge_web.db.billing_models import Checkout
from forge_web.db.models import User
from stripe_standin import TEST_KEY, WEBHOOK_SECRET, StripeStandIn, body, event, signed
from support import LiveServer, Person, dev_settings, person

PRO = {"kind": "plan", "name": "Pro", "amounts": {"month": 2900}, "trial_days": 14,
       "limits": {"projects": 5, "hosted_apps": 2}}  # fmt: skip
HOOK = "/api/billing/stripe/webhook"


@pytest.fixture
def mails(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """The mails handed to SMTP."""
    sent: list[dict[str, Any]] = []

    async def record(smtp: Any, to: str, subject: str, text: str, **headers: str) -> None:
        sent.append({"to": to, "subject": subject, "body": text})

    monkeypatch.setattr(checkout, "send_mail", record)
    return sent


@pytest.fixture
def stripe() -> StripeStandIn:
    return StripeStandIn()


@pytest.fixture
def server(tmp_path: Path, stripe: StripeStandIn) -> Iterator[LiveServer]:
    settings = dev_settings(tmp_path / "data")
    settings.billing.enabled = True
    settings.billing.required_texts = []  # the legal texts and their acceptance: test_legal
    settings.auth.smtp.host, settings.auth.smtp.from_address = "smtp.example", "pay@example.com"
    with LiveServer(settings) as live:
        live.services.billing.transport = stripe.transport()
        yield live


@pytest.fixture
async def admin(server: LiveServer) -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(base_url=server.url, headers=server.headers(), timeout=30) as c:
        keys = {"secret_key": TEST_KEY, "webhook_secret": WEBHOOK_SECRET}
        assert (await c.put("/api/admin/billing/stripe", json=keys)).status_code == 200
        assert (await c.put("/api/admin/billing/catalog/items/pro", json=PRO)).status_code == 200
        assert (await c.post("/api/admin/billing/catalog/sync")).status_code == 200
        yield c


async def deliver(server: LiveServer, data: dict[str, Any]) -> None:
    payload = body(data)
    async with httpx.AsyncClient(base_url=server.url, timeout=30) as stripe:
        sent = await stripe.post(HOOK, content=payload, headers={
            "Stripe-Signature": signed(payload), "Content-Type": "application/json"})  # fmt: skip
    assert sent.status_code == 200, sent.text


async def source(server: LiveServer, who: Person) -> str:
    async with server.services.db.session() as session:
        user = await session.get(User, who.id)
    assert user is not None
    return (await server.services.entitlements.of(user)).source


async def until(check: Any) -> Any:
    async with asyncio.timeout(20):
        while not (found := await check()):
            await asyncio.sleep(0.05)
        return found


def subscription(stripe: StripeStandIn, user_id: str, status: str, at: float) -> dict[str, Any]:
    [month] = [p for p in stripe.of("price") if p.get("recurring[interval]") == "month"]
    price = {
        "id": month["id"],
        "metadata": {"forge_item": "pro"},
        "recurring": {"interval": "month"},
    }
    sub = {"id": "sub_1", "object": "subscription", "status": status, "customer": "cus_1",
           "metadata": {"forge_user": user_id}, "current_period_end": int(at + 30 * 86400),
           "items": {"data": [{"id": "si_1", "price": price, "quantity": 1}]}}  # fmt: skip
    return sub


async def test_checkout_is_refused_without_the_waiver_box(
    server: LiveServer, admin: httpx.AsyncClient, stripe: StripeStandIn
) -> None:
    maria = await person(server, "maria")
    refused = await maria.web.post("/api/billing/checkout", {"item_id": "pro"})
    assert refused.status_code == 422 and "withdrawal" in refused.text
    assert stripe.of("cs") == []
    unknown = await maria.web.post("/api/billing/checkout", {"item_id": "gold", "waiver": True})
    assert unknown.status_code == 404
    shown = (await maria.web.get("/api/billing/catalog")).json()
    assert [i["id"] for i in shown] == ["pro"] and "prices" not in shown[0]
    await maria.web.client.aclose()


async def test_a_paid_checkout_grants_its_entitlements(
    server: LiveServer, admin: httpx.AsyncClient, stripe: StripeStandIn,
    mails: list[dict[str, Any]],
) -> None:  # fmt: skip
    maria = await person(server, "maria")
    assert await source(server, maria) == "server"
    ordered = await maria.web.post("/api/billing/checkout", {"item_id": "pro", "waiver": True})
    assert ordered.status_code == 200, ordered.text
    [session] = stripe.of("cs")
    assert ordered.json() == {"url": session["url"]}
    assert session["mode"] == "subscription" and session["automatic_tax[enabled]"] == "true"
    assert session["subscription_data[trial_period_days]"] == "14"
    assert session["line_items[0][price]"].startswith("price_")
    assert session["customer"] == stripe.of("cus")[0]["id"]
    assert stripe.of("cus")[0]["email"] == maria.email
    assert not any("card" in name for name in session)  # cards stay with Stripe
    async with server.services.db.session() as db:
        row = await db.get(Checkout, session["id"])
    assert row is not None and row.waiver_at > 0 and row.status == "open"
    now = time.time()
    done = {"id": session["id"], "object": "checkout.session", "status": "complete",
            "customer": session["customer"], "subscription": "sub_1"}  # fmt: skip
    await deliver(server, event("evt_1", "checkout.session.completed", now, done))
    await deliver(server, event("evt_2", "customer.subscription.created", now,
                                subscription(stripe, maria.id, "trialing", now)))  # fmt: skip
    assert await until(lambda: plan_of(server, maria)) == "plan:pro"
    [mail] = await until(lambda: settled(mails))
    assert mail["to"] == maria.email and "Pro" in mail["body"]
    assert "Widerrufsrecht" in mail["body"] or "withdrawal" in mail["body"]
    mine = (await maria.web.get("/api/billing/me")).json()
    assert mine["subscriptions"][0]["item_id"] == "pro" and mine["entitlements"]["hosted_apps"] == 2
    ended = subscription(stripe, maria.id, "canceled", now)
    await deliver(server, event("evt_3", "customer.subscription.deleted", now + 1, ended))
    assert await until(lambda: back_to_server(server, maria))
    await maria.web.client.aclose()


async def plan_of(server: LiveServer, who: Person) -> str:
    found = await source(server, who)
    return found if found.startswith("plan:") else ""


async def back_to_server(server: LiveServer, who: Person) -> bool:
    return await source(server, who) == "server"


async def settled(mails: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return mails


async def test_a_cancelled_checkout_grants_nothing(
    server: LiveServer, admin: httpx.AsyncClient, stripe: StripeStandIn
) -> None:
    maria = await person(server, "maria")
    await maria.web.post("/api/billing/checkout", {"item_id": "pro", "waiver": True})
    [session] = stripe.of("cs")
    gone = {"id": session["id"], "object": "checkout.session", "status": "expired"}
    await deliver(server, event("evt_9", "checkout.session.expired", time.time(), gone))

    async def expired() -> bool:
        async with server.services.db.session() as db:
            row = await db.get(Checkout, session["id"])
        return row is not None and row.status == "expired"

    assert await until(expired)
    assert await source(server, maria) == "server"
    await maria.web.client.aclose()


async def test_a_trial_comes_once_and_with_plans_only(
    server: LiveServer, admin: httpx.AsyncClient, stripe: StripeStandIn,
    mails: list[dict[str, Any]],
) -> None:  # fmt: skip
    addon = {"kind": "addon", "name": "More apps", "amounts": {"month": 500}, "trial_days": 7,
             "limits": {"hosted_apps": 1}}  # fmt: skip
    refused = await admin.put("/api/admin/billing/catalog/items/more", json=addon)
    assert refused.status_code == 422 and "only plans have a trial" in refused.text
    maria = await person(server, "maria")
    assert (await maria.web.get("/api/billing/catalog")).json()[0]["trial_days"] == 14
    await maria.web.post("/api/billing/checkout", {"item_id": "pro", "waiver": True})
    [first] = stripe.of("cs")
    assert first["subscription_data[trial_period_days]"] == "14"
    now = time.time()
    done = {"id": first["id"], "object": "checkout.session", "status": "complete",
            "payment_status": "no_payment_required", "customer": first["customer"]}  # fmt: skip
    await deliver(server, event("evt_1", "checkout.session.completed", now, done))
    await deliver(server, event("evt_2", "customer.subscription.created", now,
                                subscription(stripe, maria.id, "trialing", now)))  # fmt: skip
    assert await until(lambda: plan_of(server, maria)) == "plan:pro"
    again = await maria.web.post("/api/billing/checkout", {"item_id": "pro", "waiver": True})
    assert again.status_code == 409 and "customer portal" in again.text  # no second plan
    ended = subscription(stripe, maria.id, "canceled", now)
    await deliver(server, event("evt_3", "customer.subscription.deleted", now + 1, ended))
    assert await until(lambda: back_to_server(server, maria))
    assert (await maria.web.get("/api/billing/catalog")).json()[0]["trial_days"] == 0
    ordered = await maria.web.post("/api/billing/checkout", {"item_id": "pro", "waiver": True})
    assert ordered.status_code == 200, ordered.text
    second = stripe.of("cs")[-1]
    assert "subscription_data[trial_period_days]" not in second  # the trial was had
    await maria.web.client.aclose()


async def test_a_new_order_of_a_plan_ends_the_open_one(
    server: LiveServer, admin: httpx.AsyncClient, stripe: StripeStandIn
) -> None:
    maria = await person(server, "maria")
    for _ in range(2):
        sent = await maria.web.post("/api/billing/checkout", {"item_id": "pro", "waiver": True})
        assert sent.status_code == 200, sent.text
    first, second = stripe.of("cs")
    assert first["status"] == "expired" and second.get("status", "open") == "open"
    async with server.services.db.session() as db:
        rows = {cs["id"]: await db.get(Checkout, cs["id"]) for cs in (first, second)}
    assert [r.status for r in rows.values() if r] == ["expired", "open"]
    assert all(r.trial_days == 14 for r in rows.values() if r)  # neither was paid
    await maria.web.client.aclose()
