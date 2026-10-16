"""Stripe Connect (W35): a maker onboards as an Express account of their own, made once at Stripe
(this server never sees their identity or bank details), sees whether it may charge and be paid
out, and opens Stripe's Express dashboard. Stripe is its stand-in."""

from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from urllib.parse import parse_qsl

import httpx
import pytest

from stripe_standin import TEST_KEY, WEBHOOK_SECRET
from stripe_standin_connect import ConnectStandIn
from support import LiveServer, dev_settings, person


@pytest.fixture
def stripe() -> ConnectStandIn:
    return ConnectStandIn()


@pytest.fixture
def server(tmp_path: Path, stripe: ConnectStandIn) -> Iterator[LiveServer]:
    settings = dev_settings(tmp_path / "data")
    settings.billing.enabled = True
    settings.billing.connect_fee_percent = 10
    with LiveServer(settings) as live:
        live.services.billing.transport = stripe.transport()
        yield live


@pytest.fixture
async def admin(server: LiveServer) -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(base_url=server.url, headers=server.headers(), timeout=30) as c:
        keys = {"secret_key": TEST_KEY, "webhook_secret": WEBHOOK_SECRET}
        assert (await c.put("/api/admin/billing/stripe", json=keys)).status_code == 200
        yield c


async def test_a_maker_onboards_as_an_express_account(
    server: LiveServer, admin: httpx.AsyncClient, stripe: ConnectStandIn
) -> None:
    maria = await person(server, "maria")
    before = (await maria.web.get("/api/billing/connect")).json()
    assert before["connected"] is False and before["fee_percent"] == 10
    first = (await maria.web.post("/api/billing/connect")).json()
    assert first["url"].startswith("https://connect.stripe.com/setup/e/acct_")
    again = (await maria.web.post("/api/billing/connect")).json()
    accounts = stripe.of("acct")
    assert len(accounts) == 1 and again["url"] == first["url"]  # one account, opened again
    made = accounts[0]
    assert made["type"] == "express" and made["metadata[forge_user]"] == maria.id
    assert made["capabilities[card_payments][requested]"] == "true"
    asked = next(r for r in stripe.requests if r.url.path == "/v1/account_links")
    link = dict(parse_qsl(asked.content.decode()))
    assert link["return_url"].endswith("/billing?connect=done")
    assert link["type"] == "account_onboarding"
    shown = (await maria.web.get("/api/billing/connect")).json()
    assert shown["connected"] and not shown["charges_enabled"]
    assert made["id"] not in str(shown)  # the maker never needs the id
    assert (await maria.web.post("/api/billing/connect/dashboard")).status_code == 409
    stripe.onboarded(made["id"])
    shown = (await maria.web.get("/api/billing/connect")).json()
    assert shown["charges_enabled"] and shown["payouts_enabled"] and shown["details_submitted"]
    dashboard = (await maria.web.post("/api/billing/connect/dashboard")).json()
    assert dashboard["url"].startswith("https://connect.stripe.com/express/")
    await maria.web.client.aclose()


async def test_connecting_waits_for_payments_to_be_set_up(
    server: LiveServer, stripe: ConnectStandIn
) -> None:
    maria = await person(server, "maria")  # the admin has not entered Stripe's keys yet
    refused = await maria.web.post("/api/billing/connect")
    assert refused.status_code == 409 and "not set up" in refused.text
    assert stripe.requests == []
    await maria.web.client.aclose()
