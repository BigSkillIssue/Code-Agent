"""The admin controls every customer's billing (W40c): each customer with their plan, orders, apps,
dunning and cancellations; a plan given for free (counted like a paid one, until its date);
subscriptions ended at Stripe at once or at the period's end; a dunning case held, resumed or
closed; an app unlocked or taken back; a cancellation that waits for an admin carried out. Only
admins (with two-factor where the server asks for it), and every action in the audit log."""

import json
import time
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from sqlalchemy import select

from forge_web.billing import cancel
from forge_web.billing import dunning as dunning_module
from forge_web.db.billing_models import DunningCase, PlanGift, Subscription
from forge_web.db.models import AuditEntry, User
from stripe_standin import TEST_KEY, WEBHOOK_SECRET, StripeStandIn
from support import LiveServer, Person, dev_settings, person
from test_dunning import dunning_for

PRO = {"kind": "plan", "name": "Pro", "amounts": {"month": 2900}, "limits": {"hosted_apps": 4}}
MORE = {"kind": "addon", "name": "More", "amounts": {"month": 500}, "limits": {"projects": 1}}
BASE = "/api/admin/billing"


@pytest.fixture
def mails(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    sent: list[dict[str, Any]] = []

    async def record(smtp: Any, to: str, subject: str, text: str, **headers: str) -> None:
        sent.append({"to": to, "subject": subject, "body": text})

    monkeypatch.setattr(cancel, "send_mail", record)
    monkeypatch.setattr(dunning_module, "send_mail", record)
    return sent


@pytest.fixture
def stripe() -> StripeStandIn:
    return StripeStandIn()


@pytest.fixture
def server(tmp_path: Path, stripe: StripeStandIn) -> Iterator[LiveServer]:
    settings = dev_settings(tmp_path / "data")
    settings.billing.enabled = True
    settings.billing.required_texts = []
    settings.auth.smtp.host, settings.auth.smtp.from_address = "smtp.example", "pay@example.com"
    with LiveServer(settings) as live:
        live.services.billing.transport = stripe.transport()
        yield live


@pytest.fixture
async def admin(server: LiveServer) -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(base_url=server.url, headers=server.headers(), timeout=30) as c:
        keys = {"secret_key": TEST_KEY, "webhook_secret": WEBHOOK_SECRET}
        assert (await c.put(f"{BASE}/stripe", json=keys)).status_code == 200
        for item_id, item in {"pro": PRO, "more": MORE}.items():
            assert (await c.put(f"{BASE}/catalog/items/{item_id}", json=item)).status_code == 200
        yield c


async def source(server: LiveServer, who: Person) -> str:
    async with server.services.db.session() as session:
        user = await session.get(User, who.id)
    assert user is not None
    return (await server.services.entitlements.of(user)).source


async def actions(server: LiveServer) -> list[str]:
    async with server.services.db.session() as session:
        rows = await session.scalars(select(AuditEntry.action).where(
            AuditEntry.action.like("billing.admin_%")).order_by(AuditEntry.id))  # fmt: skip
        return list(rows)


async def subscribed(server: LiveServer, who: Person, stripe: StripeStandIn, sub: str) -> None:
    """A running subscription to Pro, at Stripe and here."""
    stripe.objects[sub] = {"object": "sub", "id": sub, "active": True}
    now = time.time()
    async with server.services.db.session() as session, session.begin():
        session.add(Subscription(id=f"si_{sub}", user_id=who.id, item_id="pro", status="active",
                                 stripe_subscription=sub, current_period_end=now + 86400,
                                 created_at=now, updated_at=now))  # fmt: skip


async def test_only_admins_see_the_customers_and_a_free_plan_counts_until_its_date(
    server: LiveServer, admin: httpx.AsyncClient
) -> None:
    maria = await person(server, "maria")
    assert (await maria.web.get(f"{BASE}/customers")).status_code == 403
    listed = (await admin.get(f"{BASE}/customers", params={"q": "MARIA"})).json()
    assert [(c["id"], c["paying"], c["gift"]) for c in listed] == [(maria.id, [], "")]
    url = f"{BASE}/customers/{maria.id}/gift"
    assert (await admin.put(url, json={"item_id": "more"})).status_code == 409  # plans only
    past = await admin.put(url, json={"item_id": "pro", "until": time.time() - 60})
    assert past.status_code == 422
    given = await admin.put(url, json={"item_id": "pro", "until": time.time() + 3600,
                                       "note": "a partner"})  # fmt: skip
    assert given.status_code == 200, given.text
    assert await source(server, maria) == "plan:pro"
    shown = (await admin.get(f"{BASE}/customers/{maria.id}")).json()
    assert shown["gift"]["note"] == "a partner" and shown["entitlements"]["hosted_apps"] == 4
    async with server.services.db.session() as session, session.begin():
        gift = await session.get(PlanGift, maria.id)
        assert gift is not None
        gift.until = time.time() - 1  # its date has come
    assert await source(server, maria) == "server"
    assert (await admin.delete(url)).status_code == 200
    assert (await admin.delete(url)).status_code == 404
    server.services.settings.auth.admin_two_factor = True  # the admin here has no second factor
    assert (await admin.get(f"{BASE}/customers")).status_code == 403
    server.services.settings.auth.admin_two_factor = False
    assert await actions(server) == ["billing.admin_gift", "billing.admin_gift_removed"]
    await maria.web.client.aclose()


async def test_the_admin_ends_subscriptions_and_holds_or_closes_dunning(
    server: LiveServer, admin: httpx.AsyncClient, stripe: StripeStandIn,
    mails: list[dict[str, Any]],
) -> None:  # fmt: skip
    maria = await person(server, "maria")
    await subscribed(server, maria, stripe, "sub_1")
    url = f"{BASE}/customers/{maria.id}/subscriptions"
    assert (await admin.post(f"{url}/sub_9/end", json={})).status_code == 404
    assert (await admin.post(f"{url}/sub_1/end", json={})).status_code == 200
    assert ("POST", "/v1/subscriptions/sub_1", {"cancel_at_period_end": "true"}) in stripe.effects
    assert await source(server, maria) == "plan:pro"  # it runs until its period ends
    assert (await admin.post(f"{url}/sub_1/end", json={"now": True})).status_code == 200
    assert ("DELETE", "/v1/subscriptions/sub_1", {}) in stripe.effects
    assert await source(server, maria) == "server"
    case_url = f"{BASE}/customers/{maria.id}/dunning"
    assert (await admin.post(f"{case_url}/hold")).status_code == 404  # no case yet
    start = time.time() - 20 * 86400
    async with server.services.db.session() as session, session.begin():
        session.add(DunningCase(id="d1", user_id=maria.id, opened_at=start, stage="grace",
                                stage_at=start, invoices=json.dumps(["in_1"])))  # fmt: skip
    assert (await admin.post(f"{case_url}/hold")).status_code == 200
    dunning = dunning_for(server)
    assert await dunning.tick(time.time()) == []  # held: it does not move
    assert (await admin.post(f"{case_url}/resume")).status_code == 200
    assert await dunning.tick(time.time()) == [("d1", "suspended")]
    shown = (await admin.get(f"{BASE}/customers/{maria.id}")).json()
    assert shown["dunning"]["stage"] == "suspended" and shown["subscriptions"][0]["status"] == (
        "canceled"
    )
    assert (await admin.post(f"{case_url}/close")).status_code == 200
    assert (await admin.get(f"{BASE}/customers/{maria.id}")).json()["dunning"]["closed_at"] > 0
    assert await actions(server) == [
        "billing.admin_subscription_ended", "billing.admin_subscription_ended",
        "billing.admin_dunning_hold", "billing.admin_dunning_resume", "billing.admin_dunning_close",
    ]  # fmt: skip
    await dunning.close()
    await maria.web.client.aclose()


async def test_the_admin_unlocks_apps_and_carries_out_a_waiting_cancellation(
    server: LiveServer, admin: httpx.AsyncClient, stripe: StripeStandIn,
    mails: list[dict[str, Any]],
) -> None:  # fmt: skip
    maria, jonas = await person(server, "maria"), await person(server, "jonas")
    made = await maria.web.request("POST", "/api/projects", json={"name": "Shop", "source": "app"})
    project_id = made.json()["id"]
    apps = f"{BASE}/customers/{maria.id}/apps/{project_id}"
    assert (await admin.post(f"{BASE}/customers/{jonas.id}/apps/{project_id}/unlock")).status_code \
        == 404  # fmt: skip
    assert (await admin.post(f"{apps}/lock")).status_code == 404  # nothing to take back
    assert (await admin.post(f"{apps}/unlock")).status_code == 200
    mine = (await maria.web.get("/api/billing/me")).json()["apps"]
    assert mine == [{"project_id": project_id, "name": "Shop", "unlocked": True}]
    shown = (await admin.get(f"{BASE}/customers/{maria.id}")).json()["apps"]
    assert shown[0]["by"] == "admin"
    assert (await admin.post(f"{apps}/lock")).status_code == 200
    assert not (await maria.web.get("/api/billing/me")).json()["apps"][0]["unlocked"]
    await subscribed(server, maria, stripe, "sub_2")
    sent = await maria.web.post("/api/billing/cancel", {"extraordinary": True, "reason": "moved"})
    assert sent.json()["status"] == "manual"  # for cause: an admin decides
    waiting = (await admin.get(f"{BASE}/cancellations")).json()["waiting"]
    assert [w["reason"] for w in waiting] == ["moved"]
    done_url = f"{BASE}/cancellations/{waiting[0]['id']}"
    done = await admin.post(done_url, json={"action": "now"})
    assert done.status_code == 200 and done.json()["status"] == "done", done.text
    assert ("DELETE", "/v1/subscriptions/sub_2", {}) in stripe.effects
    assert (await admin.post(done_url, json={"action": "nothing"})).status_code == 409
    assert (await admin.get(f"{BASE}/cancellations")).json()["waiting"] == []
    assert any("ist seit dem" in m["body"] for m in mails)  # the customer learns it took effect
    assert await actions(server) == ["billing.admin_app_unlock", "billing.admin_app_lock",
                                     "billing.admin_cancellation_done"]  # fmt: skip
    for who in (maria, jonas):
        await who.web.client.aclose()
