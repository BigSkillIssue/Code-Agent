"""The payments relay (W35): a release that asks for payments gets a token of its own; with it the
app opens Stripe Checkout as its maker's connected account, and only that one; the admin's fee is
on every charge; a suspended app's token is refused; and the platform key is never in an answer
or a log. Stripe is its stand-in, the host the stand-in host."""

import logging
import secrets
import time
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl

import httpx
import pytest

from forge_hostworker.wire import app_host
from forge_web.billing.relay_tokens import payments_token
from forge_web.db.hosting_models import HostedApp
from forge_web.db.models import Project
from forge_web.hosting.hosts import create_host
from host_standin import HostStandIn, host_online
from stripe_standin import TEST_KEY, WEBHOOK_SECRET
from stripe_standin_connect import ConnectStandIn
from support import LiveServer, Person, person
from test_hosting_deploy import (
    DOMAIN,
    app_project,
    approve_head,
    deployed,
    git,
    host_token,
    settings_for,
    settled,
    short_polls,
    until,
)

__all__ = ["short_polls"]
RELAY = "/api/relay/payments"


@pytest.fixture
def stripe() -> ConnectStandIn:
    return ConnectStandIn()


@pytest.fixture
def server(tmp_path: Path, stripe: ConnectStandIn) -> Iterator[LiveServer]:
    settings = settings_for(tmp_path / "data")
    settings.billing.enabled = True
    settings.billing.connect_fee_percent = 10
    with LiveServer(settings) as live:
        live.services.billing.transport = stripe.transport()
        yield live


@pytest.fixture
async def client(server: LiveServer) -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(base_url=server.url, headers=server.headers(), timeout=60) as c:
        keys = {"secret_key": TEST_KEY, "webhook_secret": WEBHOOK_SECRET}
        assert (await c.put("/api/admin/billing/stripe", json=keys)).status_code == 200
        yield c


@pytest.fixture
async def app_client(server: LiveServer) -> AsyncIterator[httpx.AsyncClient]:
    """A hosted app calling the relay (no session, no cookies)."""
    async with httpx.AsyncClient(base_url=server.url, timeout=30) as c:
        yield c


async def onboarded_maker(server: LiveServer, stripe: ConnectStandIn, name: str) -> Person:
    """A maker whose Express account may charge."""
    maker = await person(server, name)
    assert (await maker.web.post("/api/billing/connect")).status_code == 200
    stripe.onboarded(account_of(stripe, maker.id))
    return maker


def account_of(stripe: ConnectStandIn, user_id: str) -> str:
    return next(a["id"] for a in stripe.of("acct") if a["metadata[forge_user]"] == user_id)


async def hosted_app(server: LiveServer, owner: str, app: str) -> str:
    """A hosted app of the owner's, and its staging payments token."""
    db, now, project_id = server.services.db, time.time(), secrets.token_hex(8)
    host, _ = await create_host(db, f"host-{app}", HostStandIn().public_key)
    async with db.session() as session, session.begin():
        session.add(Project(id=project_id, name=app, owner_id=owner, source="app",
                            created_at=now, updated_at=now))  # fmt: skip
        await session.flush()
        session.add(HostedApp(project_id=project_id, app=app, host_id=host.id, created_at=now))
    return await payments_token(db, server.services.vault, project_id, "staging")


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def order(app: str, **fields: Any) -> dict[str, Any]:
    """A purchase as an app sends it, back to its own pages."""
    own = f"https://{app_host(app, 'staging', DOMAIN)}"
    return {"mode": "payment", "name": "Pro for a year", "amount": 1999, "currency": "eur",
            "success_url": f"{own}/paid?session={{CHECKOUT_SESSION_ID}}",
            "cancel_url": f"{own}/pricing", "reference": "order-7", **fields}  # fmt: skip


async def test_a_release_that_asks_for_payments_gets_its_token(
    server: LiveServer, client: httpx.AsyncClient, app_client: httpx.AsyncClient
) -> None:
    host, owner = HostStandIn(), server.services.dev_user_id
    project_id, _ = await app_project(server, client, owner)
    workspace = server.services.driver.workspace(project_id)
    manifest = workspace / "forge.app.toml"
    manifest.write_text(manifest.read_text() + '\n[payments]\nkind = "relay"\n', encoding="utf-8")
    git(workspace, "commit", "-qam", "Take payments")
    await approve_head(server, project_id, owner)
    async with host_online(server.url, await host_token(server, host), host):
        deploy_id = await deployed(client, project_id, "staging")
        done = await until(lambda: settled(client, project_id, deploy_id))
        assert done["status"] == "live", f"{done}"
    release = host.last("release")
    assert release.plan is not None
    api = next(s for s in release.plan.services if s.name == "api")
    assert api.env["PAYMENTS_RELAY_URL"].endswith(RELAY)
    web = next(s for s in release.plan.services if s.name == "web")
    assert "PAYMENTS_RELAY_TOKEN" not in web.secrets  # the static site gets no secrets
    token = host.secrets[release.id]["PAYMENTS_RELAY_TOKEN"]
    asked = await app_client.get(f"{RELAY}/checkout/cs_1", headers=bearer(token))
    assert asked.status_code == 409  # a valid token; its maker has not connected Stripe yet
    assert (await app_client.get(f"{RELAY}/checkout/cs_1", headers=bearer(token[:-2] + "xx"))
            ).status_code == 401  # fmt: skip


async def test_an_app_token_reaches_only_its_own_account(
    server: LiveServer, client: httpx.AsyncClient, app_client: httpx.AsyncClient,
    stripe: ConnectStandIn,
) -> None:  # fmt: skip
    maria = await onboarded_maker(server, stripe, "maria")
    tom = await onboarded_maker(server, stripe, "tom")
    shop, blog = (
        await hosted_app(server, maria.id, "shop"),
        await hosted_app(server, tom.id, "blog"),
    )
    opened = await app_client.post(
        f"{RELAY}/checkout",
        headers=bearer(shop),
        json=order("shop", mode="subscription", interval="month"),
    )
    assert opened.status_code == 200, opened.text
    checkout = opened.json()
    assert checkout["url"].startswith("https://checkout.stripe.com/")
    assert stripe.objects[checkout["id"]]["account"] == account_of(stripe, maria.id)
    assert (await app_client.get(f"{RELAY}/checkout/{checkout['id']}", headers=bearer(blog))
            ).status_code == 404  # fmt: skip
    subscription = stripe.paid(checkout["id"])["subscription"]
    state = (
        await app_client.get(f"{RELAY}/checkout/{checkout['id']}", headers=bearer(shop))
    ).json()
    assert state["status"] == "complete" and state["subscription"] == subscription
    assert state["reference"] == "order-7" and state["customer_email"] == "kim@example.org"
    other = f"{RELAY}/subscriptions/{subscription}"
    assert (await app_client.get(other, headers=bearer(blog))).status_code == 404
    assert (await app_client.post(f"{other}/cancel", headers=bearer(blog))).status_code == 404
    assert stripe.objects[subscription]["cancel_at_period_end"] is False
    ended = (await app_client.post(f"{other}/cancel", headers=bearer(shop))).json()
    assert ended["cancel_at_period_end"] and ended["current_period_end"] == 1_900_000_000
    elsewhere = order("shop", success_url="https://evil.example.org/paid")
    refused = await app_client.post(f"{RELAY}/checkout", headers=bearer(shop), json=elsewhere)
    assert refused.status_code == 422  # customers go back to the app's own pages only
    await maria.web.client.aclose()
    await tom.web.client.aclose()


async def test_the_admins_fee_is_on_every_charge(
    server: LiveServer, client: httpx.AsyncClient, app_client: httpx.AsyncClient,
    stripe: ConnectStandIn,
) -> None:  # fmt: skip
    maria = await onboarded_maker(server, stripe, "maria")
    shop = await hosted_app(server, maria.id, "shop")
    once = order("shop", amount=1999, quantity=2)
    monthly = order("shop", mode="subscription", interval="month", amount=990)
    for body in (once, monthly):
        made = await app_client.post(f"{RELAY}/checkout", headers=bearer(shop), json=body)
        assert made.status_code == 200, made.text
    opened = [r for r in stripe.requests if r.url.path == "/v1/checkout/sessions"]
    params = [dict(parse_qsl(r.content.decode())) for r in opened]
    assert len(params) == 2 == len(stripe.sessions())
    assert params[0]["payment_intent_data[application_fee_amount]"] == "400"  # 10% of 39.98
    assert params[1]["subscription_data[application_fee_percent]"] == "10"
    assert all(r.headers["stripe-account"] == account_of(stripe, maria.id) for r in opened)
    await maria.web.client.aclose()


async def test_a_suspended_apps_token_is_refused(
    server: LiveServer, client: httpx.AsyncClient, app_client: httpx.AsyncClient,
    stripe: ConnectStandIn,
) -> None:  # fmt: skip
    maria = await onboarded_maker(server, stripe, "maria")
    shop = await hosted_app(server, maria.id, "shop")
    assert (await app_client.post(f"{RELAY}/checkout", headers=bearer(shop), json=order("shop"))
            ).status_code == 200  # fmt: skip
    for stopped in ({"suspended": True}, {"suspended": False, "deleting_at": time.time()}):
        async with server.services.db.session() as session, session.begin():
            app = await session.get(HostedApp, shop.split("_")[1])
            assert app is not None
            for name, value in stopped.items():
                setattr(app, name, value)
        refused = await app_client.post(f"{RELAY}/checkout", headers=bearer(shop),
                                        json=order("shop"))  # fmt: skip
        assert refused.status_code == 403, stopped
    assert len(stripe.sessions()) == 1
    tom = await person(server, "tom")  # never connected Stripe
    blog = await hosted_app(server, tom.id, "blog")
    waiting = await app_client.post(f"{RELAY}/checkout", headers=bearer(blog), json=order("blog"))
    assert waiting.status_code == 409 and "Stripe" in waiting.text
    await maria.web.client.aclose()
    await tom.web.client.aclose()


async def test_the_platform_key_never_leaves_the_server(
    server: LiveServer, client: httpx.AsyncClient, app_client: httpx.AsyncClient,
    stripe: ConnectStandIn, caplog: pytest.LogCaptureFixture,
) -> None:  # fmt: skip
    caplog.set_level(logging.DEBUG)
    maria = await onboarded_maker(server, stripe, "maria")
    shop = await hosted_app(server, maria.id, "shop")
    answers = [await app_client.post(f"{RELAY}/checkout", headers=bearer(shop), json=order("shop"))]
    checkout = answers[0].json()["id"]
    answers.append(await app_client.get(f"{RELAY}/checkout/{checkout}", headers=bearer(shop)))
    stripe.repeat_key = True  # Stripe's error repeats the key it was called with
    answers.append(await app_client.post(f"{RELAY}/checkout", headers=bearer(shop),
                                         json=order("shop")))  # fmt: skip
    answers.append(await app_client.get(f"{RELAY}/checkout/{checkout}", headers=bearer(shop)))
    answers.append(await maria.web.get("/api/billing/connect"))
    assert [a.status_code for a in answers] == [200, 200, 502, 502, 200]
    for answer in answers:
        assert TEST_KEY not in answer.text and "sk_test" not in answer.text
    assert TEST_KEY not in caplog.text and TEST_KEY[8:] not in caplog.text
    await maria.web.client.aclose()
