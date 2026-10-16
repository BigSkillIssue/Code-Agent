"""Legal setup (W34): the admin publishes versioned terms, privacy policy, data processing
agreement and acceptable use policy; nothing paid is possible before a customer accepted the
current versions; a new version asks again; every acceptance names the version and the time; the
texts are public; an EU VAT ID is checked with VIES (here its stand-in)."""

from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from sqlalchemy import select

from forge_web.db.billing_models import LegalAcceptance, VatCheck
from stripe_standin import TEST_KEY, WEBHOOK_SECRET, StripeStandIn
from support import LiveServer, dev_settings, person
from vies_standin import ViesStandIn

KINDS = ("terms", "privacy", "dpa", "aup")
PRO = {"kind": "plan", "name": "Pro", "amounts": {"month": 2900}}


@pytest.fixture
def stripe() -> StripeStandIn:
    return StripeStandIn()


@pytest.fixture
def vies() -> Iterator[ViesStandIn]:
    with ViesStandIn() as stand_in:
        yield stand_in


@pytest.fixture
def server(tmp_path: Path, stripe: StripeStandIn, vies: ViesStandIn) -> Iterator[LiveServer]:
    settings = dev_settings(tmp_path / "data")
    settings.billing.enabled = True
    settings.billing.vies_url = vies.url
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


async def publish(admin: httpx.AsyncClient, kind: str, body: str = "") -> dict[str, Any]:
    text = {"title": f"{kind} of Forge", "body": body or f"The {kind}, as the lawyer wrote it."}
    made = await admin.post(f"/api/admin/legal/{kind}", json=text)
    assert made.status_code == 200, made.text
    return dict(made.json())


async def test_nothing_paid_is_possible_before_acceptance(
    server: LiveServer, admin: httpx.AsyncClient, stripe: StripeStandIn
) -> None:
    maria = await person(server, "maria")
    order = {"item_id": "pro", "waiver": True}
    refused = await maria.web.post("/api/billing/checkout", order)
    assert refused.status_code == 409 and "terms" in refused.text  # none published yet
    for kind in KINDS:
        assert (await publish(admin, kind))["version"] == 1
    mine = (await maria.web.get("/api/legal/me")).json()
    assert [(t["kind"], t["version"]) for t in mine["missing"]] == [(k, 1) for k in KINDS]
    assert (await maria.web.post("/api/billing/checkout", order)).status_code == 409
    for kind in KINDS[:3]:
        assert (await maria.web.post("/api/legal/accept", {"kind": kind, "version": 1})).json() == {
            "accepted": True}  # fmt: skip
    still = await maria.web.post("/api/billing/checkout", order)
    assert still.status_code == 409 and "aup" in still.text and stripe.of("cs") == []
    await maria.web.post("/api/legal/accept", {"kind": "aup", "version": 1})
    assert (await maria.web.post("/api/billing/checkout", order)).status_code == 200
    async with server.services.db.session() as session:
        rows = list(await session.scalars(select(LegalAcceptance).where(
            LegalAcceptance.user_id == maria.id)))  # fmt: skip
    assert sorted((r.kind, r.version) for r in rows) == sorted((k, 1) for k in KINDS)
    assert all(r.accepted_at > 0 for r in rows)  # each with its time
    await maria.web.client.aclose()


async def test_a_new_version_asks_again(server: LiveServer, admin: httpx.AsyncClient) -> None:
    maria = await person(server, "maria")
    for kind in KINDS:
        await publish(admin, kind)
        await maria.web.post("/api/legal/accept", {"kind": kind, "version": 1})
    assert (await maria.web.get("/api/legal/me")).json()["missing"] == []
    assert (await publish(admin, "terms", "New terms, from next month."))["version"] == 2
    missing = (await maria.web.get("/api/legal/me")).json()["missing"]
    assert [(t["kind"], t["version"], t["title"]) for t in missing] == [
        ("terms", 2, "terms of Forge")]  # fmt: skip
    old = await maria.web.post("/api/legal/accept", {"kind": "terms", "version": 1})
    assert old.status_code == 409  # only the current version can be accepted
    assert (await maria.web.post("/api/billing/checkout", {"item_id": "pro", "waiver": True})
            ).status_code == 409  # fmt: skip
    await maria.web.post("/api/legal/accept", {"kind": "terms", "version": 2})
    assert (await maria.web.get("/api/legal/me")).json()["missing"] == []
    listed = (await admin.get("/api/admin/legal")).json()
    assert [(t["kind"], t["version"]) for t in listed if t["kind"] == "terms"] == [
        ("terms", 2), ("terms", 1)]  # fmt: skip
    await maria.web.client.aclose()


async def test_the_texts_are_public_and_shown_as_text(
    server: LiveServer, admin: httpx.AsyncClient
) -> None:
    await publish(admin, "terms", "§1 <script>alert(1)</script>\n§2 Payment")
    async with httpx.AsyncClient(base_url=server.url, timeout=30) as stranger:
        page = await stranger.get("/rechtstexte/terms")
        assert page.status_code == 200 and "§2 Payment" in page.text
        assert "<script>" not in page.text and "&lt;script&gt;" in page.text
        assert "Version 1" in page.text
        assert (await stranger.get("/rechtstexte/privacy")).status_code == 404  # not yet
        assert (await stranger.get("/rechtstexte/other")).status_code == 404
        refused = await stranger.post("/api/admin/legal/terms", json={"title": "x", "body": "y"})
        assert refused.status_code in (401, 403)
    maria = await person(server, "maria")
    assert (await maria.web.post("/api/admin/legal/terms", {"title": "x", "body": "y"})
            ).status_code == 403  # fmt: skip
    await maria.web.client.aclose()


async def test_a_vat_id_is_checked_with_vies(server: LiveServer, vies: ViesStandIn) -> None:
    vies.known["DE123456789"] = ("Muster GmbH", "Hauptstraße 1\n10115 Berlin")
    maria = await person(server, "maria")
    good = (await maria.web.post("/api/legal/vat", {"vat_id": "de 123 456 789"})).json()
    assert good == {"vat_id": "DE123456789", "valid": True, "name": "Muster GmbH",
                    "address": "Hauptstraße 1\n10115 Berlin"}  # fmt: skip
    assert vies.asked == ["DE123456789"]
    unknown = (await maria.web.post("/api/legal/vat", {"vat_id": "FR12345678901"})).json()
    assert unknown["valid"] is False
    assert (await maria.web.post("/api/legal/vat", {"vat_id": "not a vat id"})).status_code == 422
    vies.down = True
    away = await maria.web.post("/api/legal/vat", {"vat_id": "DE123456789"})
    assert away.status_code == 502 and "VIES" in away.text
    async with server.services.db.session() as session:
        kept = await session.get(VatCheck, maria.id)
    assert kept is not None and kept.vat_id == "FR12345678901" and not kept.valid
    await maria.web.client.aclose()
