"""Cancelling (W33b1): the cancellation button of §312k BGB takes two steps, works without
signing in (and without signing in again for those who are), ends the contract at Stripe at the
end of the paid period and confirms receipt by mail at once, with the time it came in and when it
takes effect; what cannot be matched goes to the admins. Stripe's customer portal is a click
away. Stripe is `stripe_standin.py`."""

import asyncio
import re
import time
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from sqlalchemy import select

from forge_web.billing import cancel
from forge_web.db.billing_models import BillingCustomer, Cancellation, Subscription
from stripe_standin import TEST_KEY, WEBHOOK_SECRET, StripeStandIn
from support import LiveServer, Person, dev_settings, person

PERIOD_END = 1_900_000_000.0


@pytest.fixture
def mails(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    sent: list[dict[str, Any]] = []

    async def record(smtp: Any, to: str, subject: str, text: str, **headers: str) -> None:
        sent.append({"to": to, "subject": subject, "body": text})

    monkeypatch.setattr(cancel, "send_mail", record)
    return sent


@pytest.fixture
def stripe() -> StripeStandIn:
    return StripeStandIn()


@pytest.fixture
def server(tmp_path: Path, stripe: StripeStandIn) -> Iterator[LiveServer]:
    settings = dev_settings(tmp_path / "data")
    settings.billing.enabled = True
    settings.auth.smtp.host, settings.auth.smtp.from_address = "smtp.example", "pay@example.com"
    with LiveServer(settings) as live:
        live.services.billing.transport = stripe.transport()
        yield live


@pytest.fixture
async def keys(server: LiveServer) -> AsyncIterator[None]:
    async with httpx.AsyncClient(base_url=server.url, headers=server.headers(), timeout=30) as c:
        body = {"secret_key": TEST_KEY, "webhook_secret": WEBHOOK_SECRET}
        assert (await c.put("/api/admin/billing/stripe", json=body)).status_code == 200
    yield


async def customer(server: LiveServer, stripe: StripeStandIn, who: Person) -> None:
    """A customer with a running subscription (at Stripe and here)."""
    stripe.objects["sub_1"] = {"id": "sub_1", "object": "sub", "status": "active", "active": True}
    now = time.time()
    async with server.services.db.session() as session, session.begin():
        session.add(BillingCustomer(user_id=who.id, stripe_customer="cus_9", created_at=now))
        session.add(Subscription(id="si_1", user_id=who.id, item_id="pro", status="active",
                                 stripe_subscription="sub_1", current_period_end=PERIOD_END,
                                 created_at=now, updated_at=now))  # fmt: skip


async def until(check: Any) -> Any:
    async with asyncio.timeout(20):
        while not (found := await check()):
            await asyncio.sleep(0.05)
        return found


async def test_a_signed_in_customer_cancels_in_two_steps_without_signing_in_again(
    server: LiveServer, keys: None, stripe: StripeStandIn, mails: list[dict[str, Any]]
) -> None:
    maria = await person(server, "maria")
    await customer(server, stripe, maria)
    done = await maria.web.post("/api/billing/cancel", {})
    assert done.status_code == 200, done.text
    shown = done.json()
    assert shown["status"] == "done" and shown["effective_at"] == PERIOD_END
    assert stripe.objects["sub_1"]["cancel_at_period_end"] == "true"
    [mail] = await until(lambda: listed(mails))
    assert mail["to"] == maria.email and "eingegangen" in mail["body"]
    assert time.strftime("%d.%m.%Y", time.gmtime(PERIOD_END)) in mail["body"]
    assert time.strftime("%d.%m.%Y", time.gmtime(shown["received_at"])) in mail["body"]
    await maria.web.client.aclose()


async def listed(mails: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return mails


async def test_the_button_works_without_signing_in(
    server: LiveServer, keys: None, stripe: StripeStandIn, mails: list[dict[str, Any]]
) -> None:
    maria = await person(server, "maria")
    await customer(server, stripe, maria)
    async with httpx.AsyncClient(base_url=server.url, timeout=30) as stranger:
        first = await stranger.get("/kuendigen")
        assert first.status_code == 200 and "Verträge hier kündigen" in first.text
        second = await stranger.get("/kuendigen", params={"schritt": "2"})
        assert "Jetzt kündigen" in second.text and 'name="email"' in second.text
        incomplete = await stranger.post("/kuendigen", data={"name": "Maria"})
        assert incomplete.status_code == 400 and "Jetzt kündigen" in incomplete.text
        form = {"name": "Maria Muster", "email": maria.email.upper(), "contract": ""}
        confirmed = await stranger.post("/kuendigen", data=form)
        assert confirmed.status_code == 200 and "eingegangen" in confirmed.text
        # The page tells a stranger nothing about the account: not even when its period ends.
        assert time.strftime("%d.%m.%Y", time.gmtime(PERIOD_END)) not in confirmed.text
        unknown = await stranger.post(
            "/kuendigen", data={"name": "Kim", "email": "kim@example.org"}
        )
        assert unknown.status_code == 200 and "eingegangen" in unknown.text
        assert strip_id(unknown.text, "Kim") == strip_id(confirmed.text, "Maria Muster")
    assert stripe.objects["sub_1"]["cancel_at_period_end"] == "true"
    await until(lambda: enough(mails, 3))  # maria, kim, and the admins about kim's
    by_to = {m["to"]: m for m in mails}
    assert "kim@example.org" in by_to and maria.email in by_to
    mine = by_to[maria.email]["body"]
    assert time.strftime("%d.%m.%Y", time.gmtime(PERIOD_END)) in mine
    link = mine.split("/kuendigen/zuruecknehmen/")[1].split()[0]
    assert "zuruecknehmen" not in by_to["kim@example.org"]["body"]  # nothing to take back
    async with httpx.AsyncClient(base_url=server.url, timeout=30) as owner:
        url = f"/kuendigen/zuruecknehmen/{link}"
        assert "Kündigung zurücknehmen" in (await owner.get(url)).text
        assert (await owner.post(url)).status_code == 200
        assert stripe.objects["sub_1"]["cancel_at_period_end"] == "false"  # it runs on
        assert (await owner.post(url)).status_code == 404  # once
        assert (await owner.post("/kuendigen/zuruecknehmen/guessed")).status_code == 404
    async with server.services.db.session() as session:
        rows = {r.email: r for r in await session.scalars(select(Cancellation))}
    assert rows[maria.email].status == "undone" and rows[maria.email].user_id == maria.id
    assert rows["kim@example.org"].status == "manual"
    await maria.web.client.aclose()


def strip_id(text: str, name: str) -> str:
    """A receipt page without what differs between two cancellations: name, address, time."""
    kept = text.split("Vorgang:")[0].replace(name, "N")
    return re.sub(r"E-Mail: [^<\n]*|am [0-9.]+ um [0-9:]+ UTC", "", kept)


async def enough(mails: list[dict[str, Any]], count: int) -> bool:
    return len(mails) >= count


async def test_too_many_cancellations_from_one_address_are_slowed_down(
    server: LiveServer, keys: None, mails: list[dict[str, Any]]
) -> None:
    async with httpx.AsyncClient(base_url=server.url, timeout=30) as stranger:
        answers = []
        for n in range(12):
            sent = await stranger.post(
                "/kuendigen", data={"name": "Spam", "email": f"x{n}@example.org"}
            )
            answers.append(sent.status_code)
    assert answers[0] == 200 and answers[-1] == 429


async def test_the_customer_portal_is_one_click_away(
    server: LiveServer, keys: None, stripe: StripeStandIn
) -> None:
    maria = await person(server, "maria")
    assert (await maria.web.post("/api/billing/portal", {})).status_code == 409  # no customer yet
    await customer(server, stripe, maria)
    opened = await maria.web.post("/api/billing/portal", {})
    assert opened.status_code == 200
    [portal] = stripe.of("bps")
    assert opened.json() == {"url": portal["url"]} and portal["customer"] == "cus_9"
    assert portal["return_url"].endswith("/billing")
    await maria.web.client.aclose()
