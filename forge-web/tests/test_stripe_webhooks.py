"""Stripe's webhooks and keys (W31): the admin's keys go into the vault and are never shown
again (test keys first); an event counts only with Stripe's signature and a fresh timestamp; each
event is stored once and handled once, in the order Stripe made them, and an older event that
comes after a newer one of the same object no longer changes it."""

import asyncio
import logging
import time
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from sqlalchemy import select

from forge_web.billing.events import Events
from forge_web.db.billing_models import BillingEvent, BillingKeys
from stripe_standin import LIVE_KEY, TEST_KEY, WEBHOOK_SECRET, body, event, signed
from support import LiveServer, dev_settings, person

KEYS = "/api/admin/billing/stripe"
HOOK = "/api/billing/stripe/webhook"


@pytest.fixture
def server(tmp_path: Path) -> Iterator[LiveServer]:
    settings = dev_settings(tmp_path / "data")
    settings.billing.enabled = True
    with LiveServer(settings) as live:
        yield live


@pytest.fixture
async def client(server: LiveServer) -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(base_url=server.url, headers=server.headers(), timeout=30) as c:
        yield c


async def with_keys(client: httpx.AsyncClient) -> None:
    saved = await client.put(KEYS, json={"secret_key": TEST_KEY, "webhook_secret": WEBHOOK_SECRET})
    assert saved.status_code == 200, saved.text


async def deliver(server: LiveServer, payload: bytes, signature: str) -> httpx.Response:
    async with httpx.AsyncClient(base_url=server.url, timeout=30) as stripe:  # no session
        return await stripe.post(HOOK, content=payload, headers={
            "Stripe-Signature": signature, "Content-Type": "application/json"})  # fmt: skip


async def test_keys_go_into_the_vault_and_are_never_shown_again(
    server: LiveServer, client: httpx.AsyncClient, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    assert (await client.get(KEYS)).json()["configured"] is False
    live = await client.put(KEYS, json={"secret_key": LIVE_KEY, "webhook_secret": WEBHOOK_SECRET})
    assert live.status_code == 409 and "live" in live.text  # test mode first
    wrong = await client.put(KEYS, json={"secret_key": "pk_test_123", "webhook_secret": "x"})
    assert wrong.status_code == 422
    await with_keys(client)
    shown = (await client.get(KEYS)).json()
    assert shown["configured"] and shown["mode"] == "test" and shown["hint"] == TEST_KEY[-4:]
    assert shown["webhook_url"].endswith(HOOK)
    async with server.services.db.session() as session:
        row = await session.get(BillingKeys, 1)
    assert row is not None
    for stored in (row.secret_key, row.webhook_secret):
        assert TEST_KEY not in stored and WEBHOOK_SECRET not in stored
    maria = await person(server, "maria")
    assert (await maria.web.get(KEYS)).status_code == 403
    await maria.web.client.aclose()
    for secret in (TEST_KEY, WEBHOOK_SECRET):
        assert secret not in str(shown) and secret not in caplog.text


async def test_only_signed_and_fresh_events_count(
    server: LiveServer, client: httpx.AsyncClient
) -> None:
    payload = body(event("evt_1", "customer.created", time.time(), {"id": "cus_1"}))
    assert (await deliver(server, payload, signed(payload))).status_code == 503  # no keys yet
    await with_keys(client)
    refused = [
        signed(payload, secret="_".join(("whsec", "someone", "else"))),
        signed(payload, at=time.time() - 600),  # an old delivery, replayed
        signed(payload, at=time.time() + 600),
        signed(payload.replace(b"cus_1", b"cus_2")).replace("t=", "t=1"),
        "t=123",
        "",
    ]
    for signature in refused:
        assert (await deliver(server, payload, signature)).status_code == 400, signature
    changed = payload.replace(b"cus_1", b"cus_9")
    assert (await deliver(server, changed, signed(payload))).status_code == 400
    assert (await deliver(server, payload, signed(payload))).json() == {"received": True}
    async with server.services.db.session() as session:
        rows = list(await session.scalars(select(BillingEvent)))
    assert [r.id for r in rows] == ["evt_1"]


async def test_a_repeated_event_is_handled_once(
    server: LiveServer, client: httpx.AsyncClient
) -> None:
    await with_keys(client)
    seen: list[str] = []

    async def handler(data: dict[str, Any]) -> None:
        seen.append(data["id"])

    server.services.billing.events.on("customer.created", handler)
    payload = body(event("evt_1", "customer.created", time.time(), {"id": "cus_1"}))
    for _ in range(3):  # Stripe delivers again when it is not sure
        assert (await deliver(server, payload, signed(payload))).status_code == 200
    async with asyncio.timeout(10):
        while not seen:
            await asyncio.sleep(0.05)
    await asyncio.sleep(0.3)  # time for a second handling, if there were one
    assert seen == ["evt_1"]


async def test_events_are_handled_in_the_order_stripe_made_them(server: LiveServer) -> None:
    # Events of their own on this test's loop, on the server's database.
    events = Events(server.services.db)
    done: list[tuple[str, str]] = []

    async def handler(data: dict[str, Any]) -> None:
        done.append((data["id"], data["data"]["object"]["status"]))

    events.on("customer.subscription.updated", handler)
    now = time.time()
    sub = "sub_1"
    later = event("evt_b", "customer.subscription.updated", now, {"id": sub, "status": "active"})
    first = event("evt_a", "customer.subscription.updated", now - 60,
                  {"id": sub, "status": "incomplete"})  # fmt: skip
    assert await events.store(later) and await events.store(first)  # out of order
    assert not await events.store(first)  # stored once
    assert await events.handle_pending() == ["evt_a", "evt_b"]
    assert done == [("evt_a", "incomplete"), ("evt_b", "active")]
    stale = event("evt_c", "customer.subscription.updated", now - 30,
                  {"id": sub, "status": "past_due"})  # fmt: skip
    assert await events.store(stale)  # older than what was applied: it changes nothing
    assert await events.handle_pending() == ["evt_c"] and len(done) == 2
    async with server.services.db.session() as session:
        rows = {r.id: r for r in await session.scalars(select(BillingEvent))}
    assert rows["evt_c"].outcome == "superseded" and rows["evt_a"].outcome == "handled"
    other = event("evt_d", "invoice.paid", now, {"id": "in_1"})
    assert await events.store(other) and await events.handle_pending() == ["evt_d"]
    async with server.services.db.session() as session:
        unknown = await session.get(BillingEvent, "evt_d")
    assert unknown is not None and unknown.outcome == "ignored"  # no handler for it yet
