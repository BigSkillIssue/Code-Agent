"""Stripe's client (W31): a call that is tried again carries the same idempotency key, so Stripe
does it once; Stripe's form encoding; Stripe's errors in its own words; the key never in an
error, a log line or the client's repr; test keys first. Stripe is `stripe_standin.py`."""

import logging
import time
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from forge_web.billing import stripe_client
from forge_web.billing.setup import Billing, StripeKeysIn
from forge_web.billing.stripe_client import StripeClient, StripeError, form, key_mode
from forge_web.db.billing_models import BillingCustomer, Subscription
from forge_web.db.engine import Database
from forge_web.db.models import User
from forge_web.vault import Vault
from stripe_standin import LIVE_KEY, TEST_KEY, StripeStandIn
from support import dev_settings


@pytest.fixture(autouse=True)
def no_waiting(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(stripe_client, "WAIT_S", (0.0, 0.0, 0.0))


@pytest.fixture
def stripe() -> StripeStandIn:
    return StripeStandIn()


@pytest.fixture
async def client(stripe: StripeStandIn) -> AsyncIterator[StripeClient]:
    made = StripeClient(TEST_KEY, transport=stripe.transport())
    yield made
    await made.close()


async def test_a_retried_call_is_not_repeated(stripe: StripeStandIn, client: StripeClient) -> None:
    stripe.trouble = ["lost", 503]  # done but the answer lost, then Stripe in trouble
    customer = await client.post("/v1/customers", {"email": "ada@example.com"})
    assert customer["id"] == "cus_1" and len(stripe.effects) == 1  # made once
    keys = {r.headers["idempotency-key"] for r in stripe.requests}
    assert len(stripe.requests) == 3 and len(keys) == 1 and len(keys.pop()) >= 32
    again = await client.post("/v1/customers", {"email": "ada@example.com"}, idempotency_key="k1")
    same = await client.post("/v1/customers", {"email": "ada@example.com"}, idempotency_key="k1")
    assert again == same and len(stripe.effects) == 2  # a key of our own is kept as well


async def test_reads_carry_no_idempotency_key(stripe: StripeStandIn, client: StripeClient) -> None:
    assert (await client.get("/v1/balance"))["object"] == "balance"
    assert "idempotency-key" not in stripe.requests[-1].headers
    assert stripe.requests[-1].headers["stripe-version"] == stripe_client.VERSION


def test_nested_parameters_are_form_encoded() -> None:
    params = {"email": "a@b.c", "metadata": {"app": "shop"}, "expand": ["customer"],
              "line_items": [{"price": "price_1", "quantity": 2}], "trial": True,
              "none": None}  # fmt: skip
    assert list(form(params)) == [
        ("email", "a@b.c"), ("metadata[app]", "shop"), ("expand[0]", "customer"),
        ("line_items[0][price]", "price_1"), ("line_items[0][quantity]", "2"), ("trial", "true"),
    ]  # fmt: skip


async def test_stripes_errors_come_in_its_words_without_the_key(
    stripe: StripeStandIn, client: StripeClient, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    with pytest.raises(StripeError) as declined:
        await client.post("/v1/payment_intents", {"amount": 500, "currency": "eur"})
    assert declined.value.status == 402 and declined.value.code == "card_declined"
    assert str(declined.value) == "Your card was declined."
    stripe.trouble = ["down"] * 4
    with pytest.raises(StripeError) as away:
        await client.get("/v1/balance")
    assert away.value.status == 0 and "not reachable" in str(away.value)
    wrong = StripeClient(TEST_KEY[:-4] + "zzzz", transport=stripe.transport())
    with pytest.raises(StripeError) as refused:
        await wrong.get("/v1/balance")
    await wrong.close()
    assert refused.value.status == 401
    for shown in (str(declined.value), str(away.value), str(refused.value), repr(client),
                  repr(wrong), caplog.text):  # fmt: skip
        assert TEST_KEY not in shown and TEST_KEY[8:] not in shown


def test_test_keys_first() -> None:
    assert key_mode(TEST_KEY) == "test" and key_mode(LIVE_KEY) == "live"
    for bad in ("pk_test_abc", "sk_test_", "whsec_123", "", "sk_prod_" + "x" * 30):
        with pytest.raises(ValueError):
            key_mode(bad)


async def test_going_live_forgets_everything_test_mode_made(tmp_path: Path) -> None:
    settings = dev_settings(tmp_path / "data")
    settings.billing.allow_live = True
    db = Database(f"sqlite+aiosqlite:///{tmp_path / 'b.db'}")
    await db.migrate()
    billing = Billing(db, Vault(b"m" * 32), settings)
    hook = "whsec_" + "x" * 24
    await billing.save(StripeKeysIn(secret_key="sk_test_" + "a" * 24, webhook_secret=hook), "a")
    now = time.time()
    async with db.session() as session, session.begin():
        session.add(User(id="u1", email="u@x", role="member", status="active", created_at=now))
        await session.flush()
        session.add(BillingCustomer(user_id="u1", stripe_customer="cus_test", created_at=now))
        session.add(Subscription(id="si_1", user_id="u1", item_id="pro", status="active",
                                 created_at=now, updated_at=now))  # fmt: skip
    await billing.save(StripeKeysIn(secret_key="sk_live_" + "b" * 24, webhook_secret=hook), "a")
    async with db.session() as session:
        assert await session.get(Subscription, "si_1") is None  # a test card pays for nothing
        assert await session.get(BillingCustomer, "u1") is None
    with pytest.raises(PermissionError):
        await billing.save(StripeKeysIn(secret_key="sk_test_" + "c" * 24, webhook_secret=hook), "a")
    await db.close()
