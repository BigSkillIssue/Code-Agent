"""An app's one-time price (W40b): the order names one of the customer's own app projects; only
a paid order unlocks it (a delayed SEPA payment once it arrived, a failed one never), a full
refund takes it back, and while the admin sells a one-time price per app a customer's app goes
live in production only once it is paid for. Staging stays free."""

import time
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest

from forge_web.db.billing_models import Checkout
from host_standin import HostStandIn, host_online
from stripe_standin import TEST_KEY, WEBHOOK_SECRET, StripeStandIn, event
from support import LiveServer, Person, person
from test_checkout import deliver, mails, until
from test_hosting_deploy import (
    app_project,
    deployed,
    host_token,
    settings_for,
    settled,
    short_polls,
)

__all__ = ["mails", "short_polls"]

FREE = {"kind": "plan", "name": "Free", "amounts": {"month": 100}, "limits": {"hosted_apps": 3}}
APP = {"kind": "app_once", "name": "App", "amounts": {"once": 4900}}


@pytest.fixture
def stripe() -> StripeStandIn:
    return StripeStandIn()


@pytest.fixture
def server(tmp_path: Path, stripe: StripeStandIn) -> Iterator[LiveServer]:
    settings = settings_for(tmp_path / "data")
    settings.billing.enabled, settings.billing.free_plan = True, "free"
    settings.billing.required_texts = []
    settings.auth.smtp.host, settings.auth.smtp.from_address = "smtp.example", "pay@example.com"
    with LiveServer(settings) as live:
        live.services.billing.transport = stripe.transport()
        yield live


@pytest.fixture
async def admin(server: LiveServer) -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(base_url=server.url, headers=server.headers(), timeout=60) as c:
        keys = {"secret_key": TEST_KEY, "webhook_secret": WEBHOOK_SECRET}
        assert (await c.put("/api/admin/billing/stripe", json=keys)).status_code == 200
        for item_id, item in {"free": FREE, "app": APP}.items():
            put = await c.put(f"/api/admin/billing/catalog/items/{item_id}", json=item)
            assert put.status_code == 200, put.text
        assert (await c.post("/api/admin/billing/catalog/sync")).status_code == 200
        yield c


def order(project_id: str = "") -> dict[str, Any]:
    return {"item_id": "app", "interval": "once", "waiver": True, "project_id": project_id}


def paid_later(session: dict[str, Any], intent: str) -> dict[str, Any]:
    """The checkout as Stripe reports it when a SEPA debit is still under way."""
    return {"id": session["id"], "object": "checkout.session", "status": "complete",
            "payment_status": "unpaid", "payment_intent": intent}  # fmt: skip


async def unlocked(who: Person, project_id: str) -> bool:
    apps = (await who.web.get("/api/billing/me")).json()["apps"]
    return any(a["project_id"] == project_id and a["unlocked"] for a in apps)


async def status(server: LiveServer, checkout_id: str) -> str:
    async with server.services.db.session() as session:
        row = await session.get(Checkout, checkout_id)
    return row.status if row else ""


async def test_production_waits_for_the_apps_paid_price(
    server: LiveServer, admin: httpx.AsyncClient, stripe: StripeStandIn,
    mails: list[dict[str, Any]],
) -> None:  # fmt: skip
    host = HostStandIn()
    maria = await person(server, "maria")
    project_id, _ = await app_project(server, maria.web, maria.id)
    url = f"/api/projects/{project_id}/deploys"
    async with host_online(server.url, await host_token(server, host), host):
        staging = await deployed(maria.web, project_id, "staging")  # free to try
        assert (await until(lambda: settled(maria.web, project_id, staging)))["status"] == "live"
        refused = await maria.web.post(url, {"environment": "production"})
        assert refused.status_code == 403 and "one-time price" in refused.text
        ordered = await maria.web.post("/api/billing/checkout", order(project_id))
        assert ordered.status_code == 200, ordered.text
        [session] = stripe.of("cs")
        assert session["mode"] == "payment" and session["metadata[forge_project]"] == project_id
        now = time.time()
        await deliver(server, event("evt_1", "checkout.session.completed", now,
                                    paid_later(session, "pi_1")))  # fmt: skip
        assert await until(lambda: pending(server, session["id"]))
        assert (await maria.web.post(url, {"environment": "production"})).status_code == 403
        assert not await unlocked(maria, project_id) and mails == []  # nothing paid yet
        succeeded = {**paid_later(session, "pi_1"), "payment_status": "paid"}
        await deliver(server, event("evt_2", "checkout.session.async_payment_succeeded", now + 1,
                                    succeeded))  # fmt: skip
        assert await until(lambda: unlocked(maria, project_id))
        assert mails[0]["subject"] == "Deine Bestellung: App"
        assert (await maria.web.post(url, {"environment": "production"})).status_code == 200
        again = await maria.web.post("/api/billing/checkout", order(project_id))
        assert again.status_code == 409 and "paid for already" in again.text
    charge = {"id": "ch_1", "object": "charge", "payment_intent": "pi_1", "refunded": True}
    await deliver(server, event("evt_3", "charge.refunded", now + 2, charge))
    assert await until(lambda: refunded(server, session["id"]))
    assert not await unlocked(maria, project_id)
    await maria.web.client.aclose()


async def pending(server: LiveServer, checkout_id: str) -> bool:
    return await status(server, checkout_id) == "pending"


async def refunded(server: LiveServer, checkout_id: str) -> bool:
    return await status(server, checkout_id) == "refunded"


async def test_a_failed_payment_and_a_strangers_app_unlock_nothing(
    server: LiveServer, admin: httpx.AsyncClient, stripe: StripeStandIn,
    mails: list[dict[str, Any]],
) -> None:  # fmt: skip
    maria, jonas = await person(server, "maria"), await person(server, "jonas")
    project_id, _ = await app_project(server, maria.web, maria.id)
    unnamed = await maria.web.post("/api/billing/checkout", order())
    assert unnamed.status_code == 422  # an app's price names the app
    foreign = await jonas.web.post("/api/billing/checkout", order(project_id))
    assert foreign.status_code == 404  # only one's own apps
    plan = {"item_id": "free", "waiver": True, "project_id": project_id}
    assert (await maria.web.post("/api/billing/checkout", plan)).status_code == 422
    assert stripe.of("cs") == []
    assert (await maria.web.post("/api/billing/checkout", order(project_id))).status_code == 200
    [session] = stripe.of("cs")
    now = time.time()
    await deliver(server, event("evt_1", "checkout.session.completed", now,
                                paid_later(session, "pi_2")))  # fmt: skip
    await deliver(server, event("evt_2", "checkout.session.async_payment_failed", now + 1,
                                paid_later(session, "pi_2")))  # fmt: skip
    assert await until(lambda: failed(server, session["id"]))
    assert not await unlocked(maria, project_id)
    assert [m["subject"] for m in mails] == ["Zahlung fehlgeschlagen: App"]
    for who in (maria, jonas):
        await who.web.client.aclose()


async def failed(server: LiveServer, checkout_id: str) -> bool:
    return await status(server, checkout_id) == "failed"
