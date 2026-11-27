"""The trial (W72): a new account without a plan gets the admin's days and credit on the server's
AI keys; when either runs out, runs are refused; a mail comes three days before the end and one
at the end; the hosted apps of a trial that ended without a plan stay 14 days and then go the way
of an unpaid bill (dunning's suspension, which a paid invoice lifts); paying ends the trial. Also
the public plans the landing page shows, the start page of the product itself and the guided
start that is shown once."""

import json
import secrets
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select

from forge_web import trial as trial_module
from forge_web.auth.routes import create_user
from forge_web.auth.sessions import token_id
from forge_web.billing.dunning import TRIAL_OVER, Dunning
from forge_web.chats.one import new_chat
from forge_web.db.billing_models import CatalogItem, DunningCase, Subscription, Trial
from forge_web.db.hosting_models import HostedApp, HostServer
from forge_web.db.models import AuthSession, Project, ProjectMember, UsageRecord, User
from forge_web.gateway.meter import Ledger
from forge_web.hosting.hosts import HostQueue
from forge_web.hosting.ops import Backups
from forge_web.hosting.removal import Removals
from forge_web.trial import DAY, OVER, Trials
from support import LiveServer, WebClient, dev_settings, person

PRO = {"projects": 3, "hosted_apps": 1, "store_publishing": True}


@pytest.fixture
def server(tmp_path: Path) -> Iterator[LiveServer]:
    settings = dev_settings(tmp_path / "data")
    settings.billing.enabled = True
    settings.billing.trial_days = 10
    settings.billing.trial_credit_usd = 3
    settings.auth.smtp.host, settings.auth.smtp.from_address = "smtp.example", "hi@example.com"
    with LiveServer(settings) as live:
        yield live


@pytest.fixture
def mails(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    sent: list[dict[str, Any]] = []

    async def record(smtp: Any, to: str, subject: str, text: str, **headers: str) -> None:
        sent.append({"to": to, "subject": subject, "body": text})

    monkeypatch.setattr(trial_module, "send_mail", record)
    from forge_web.billing import dunning as dunning_module

    monkeypatch.setattr(dunning_module, "send_mail", record)
    return sent


async def newcomer(server: LiveServer, name: str) -> tuple[User, WebClient]:
    """An account made the way sign-up makes them, with a browser signed in to it."""
    user = await create_user(server.services, f"{name}@example.com", name.title(), None)
    now, token = time.time(), secrets.token_urlsafe(32)
    row = AuthSession(id=token_id(token), user_id=user.id, csrf=secrets.token_urlsafe(16),
                      created_at=now, last_seen_at=now, expires_at=now + 3600)  # fmt: skip
    async with server.services.db.session() as session, session.begin():
        session.add(row)
    web = WebClient(server)
    web.client.cookies.set("forge_session", token)
    web.client.cookies.set("forge_csrf", row.csrf)
    return user, web


def trials_for(server: LiveServer) -> Trials:
    """Trials of their own on this test's loop, with dunning of its own."""
    services = server.services
    queue = HostQueue(services.db, services.settings.data_dir, 600)
    dunning = Dunning(services.db, services.settings, queue,
                      Backups(services.db, services.vault, services.settings, queue),
                      Removals(services.db, services.settings, queue))  # fmt: skip
    return Trials(services.db, services.settings, services.entitlements, dunning)


async def trial_row(server: LiveServer, user_id: str) -> Trial:
    async with server.services.db.session() as session:
        row = await session.get(Trial, user_id)
    assert row is not None
    return row


async def spend(server: LiveServer, user_id: str, usd: float, key_kind: str = "server") -> None:
    async with server.services.db.session() as session, session.begin():
        session.add(UsageRecord(user_id=user_id, provider="anthropic", model="m", key_kind=key_kind,
                                cost_usd=usd, created_at=time.time()))  # fmt: skip


async def project_with_chat(server: LiveServer, owner: str) -> tuple[str, str]:
    now = time.time()
    project = Project(id=secrets.token_hex(8), name="Shop", owner_id=owner, created_at=now,
                      updated_at=now)  # fmt: skip
    async with server.services.db.session() as session, session.begin():
        session.add(project)
        await session.flush()
        session.add(ProjectMember(project_id=project.id, user_id=owner, role="owner"))
        chat = await new_chat(session, project)
        session.add(chat)
    return project.id, chat.id


async def subscribe(server: LiveServer, user_id: str) -> None:
    now = time.time()
    async with server.services.db.session() as session, session.begin():
        if await session.get(CatalogItem, "pro") is None:
            session.add(CatalogItem(id="pro", kind="plan", name="Pro", amounts='{"month": 2900}',
                                    limits=json.dumps(PRO), created_at=now,
                                    updated_at=now))  # fmt: skip
        session.add(Subscription(id=f"si_{user_id}", user_id=user_id, item_id="pro", quantity=1,
                                 status="active", created_at=now, updated_at=now))  # fmt: skip


async def test_a_new_account_gets_the_admins_trial(server: LiveServer) -> None:
    services = server.services
    admin = WebClient(server)
    admin.client.headers.update(server.headers())
    changed = await admin.request("PATCH", "/api/admin/settings",
                                  json={"trial_days": 7, "trial_credit_usd": 4})  # fmt: skip
    assert changed.status_code == 200, changed.text
    assert (changed.json()["trial_days"], changed.json()["trial_credit_usd"]) == (7, 4)
    before = time.time()
    maria, web = await newcomer(server, "maria")
    row = await trial_row(server, maria.id)
    assert row.ends_at == pytest.approx(before + 7 * DAY, abs=5)
    assert row.credit_usd == 4
    mine = (await web.get("/api/billing/me")).json()
    assert mine["trial"]["status"] == "active" and mine["trial"]["credit_usd"] == 4
    assert mine["entitlements"]["source"] == "trial"
    assert mine["entitlements"]["server_keys"] is True and mine["entitlements"]["hosted_apps"] == 1
    boss = await create_user(services, "boss@example.com", "Boss", None, role="admin")
    async with services.db.session() as session:
        assert await session.get(Trial, boss.id) is None  # admins have no trial
    old = await person(server, "olga")  # an account from before W72: no trial, nothing refused
    async with services.db.session() as session:
        assert await session.get(Trial, old.id) is None
    assert (await old.web.get("/api/billing/me")).json()["trial"] is None
    await admin.client.aclose()


async def test_the_credit_and_the_time_refuse_runs(server: LiveServer) -> None:
    services, trials = server.services, trials_for(server)
    maria, web = await newcomer(server, "maria")
    _, chat_id = await project_with_chat(server, maria.id)
    await spend(server, maria.id, 1.0)
    await spend(server, maria.id, 50.0, key_kind="own")  # her own keys cost the trial nothing
    state = await trials.state(maria)
    assert state is not None and state.status == "active" and state.used_usd == pytest.approx(1)
    limits = await services.entitlements.of(maria)
    ledger = Ledger(services.db)  # the gateway holds a call to what is left of the credit
    assert await ledger.reserve(maria.id, 2.5, limits.ai_month_usd) != ""
    assert await ledger.reserve(maria.id, 1.5, limits.ai_month_usd) == ""
    await spend(server, maria.id, 2.0)
    state = await trials.state(maria)
    assert state is not None and state.status == "used_up"
    assert await trials.refusal(maria) == OVER
    sent = await web.post(f"/api/chats/{chat_id}/messages", {"text": "Bau mir einen Shop"})
    assert sent.status_code == 402 and sent.json()["detail"] == OVER
    assert (await web.get("/api/billing/me")).json()["trial"]["status"] == "used_up"

    jonas, _ = await newcomer(server, "jonas")
    row = await trial_row(server, jonas.id)
    assert await trials.refusal(jonas, row.ends_at - 60) == ""
    late = await trials.state(jonas, row.ends_at + 1)
    assert late is not None and late.status == "expired"
    assert await trials.refusal(jonas, row.ends_at + 1) == OVER


async def test_mails_come_three_days_before_and_at_the_end(
    server: LiveServer, mails: list[dict[str, Any]]
) -> None:
    trials = trials_for(server)
    maria, _ = await newcomer(server, "maria")
    start = (await trial_row(server, maria.id)).started_at
    assert await trials.tick(start + 6.9 * DAY) == []
    assert await trials.tick(start + 7 * DAY) == [(maria.id, "reminded")]
    assert await trials.tick(start + 8 * DAY) == []  # once
    assert await trials.tick(start + 10 * DAY) == [(maria.id, "expired")]
    assert await trials.tick(start + 11 * DAY) == []
    assert [(m["to"], m["subject"]) for m in mails] == [
        ("maria@example.com", "Deine Testphase endet in 3 Tagen"),
        ("maria@example.com", "Deine Testphase ist vorbei"),
    ]
    assert "/billing" in mails[1]["body"]

    jonas, _ = await newcomer(server, "jonas")  # the credit can end it before its date
    await spend(server, jonas.id, 3.0)
    assert await trials.tick(time.time()) == [(jonas.id, "used_up")]
    assert mails[-1]["subject"] == "Deine Testphase ist vorbei" and len(mails) == 3


async def test_hosted_apps_stay_14_days_then_are_suspended(
    server: LiveServer, mails: list[dict[str, Any]]
) -> None:
    trials, db = trials_for(server), server.services.db
    maria, _ = await newcomer(server, "maria")
    project_id, _ = await project_with_chat(server, maria.id)
    now = time.time()
    async with db.session() as session, session.begin():
        session.add(HostServer(id="h" * 16, name="host", token_hash="0" * 64, public_key="k",
                               created_at=now))  # fmt: skip
        await session.flush()
        session.add(HostedApp(project_id=project_id, app="maria-shop", host_id="h" * 16,
                              created_at=now))  # fmt: skip
    ends = (await trial_row(server, maria.id)).ends_at
    assert await trials.tick(ends) == [(maria.id, "expired")]
    assert await trials.tick(ends + 13.9 * DAY) == []
    async with db.session() as session:
        app = await session.get(HostedApp, project_id)
    assert app is not None and not app.suspended  # still running for 14 days
    assert await trials.tick(ends + 14 * DAY) == [(maria.id, "suspended")]
    async with db.session() as session:
        app = await session.get(HostedApp, project_id)
        case = await session.scalar(select(DunningCase).where(DunningCase.user_id == maria.id))
    assert app is not None and app.suspended and app.suspended_reason == TRIAL_OVER
    assert case is not None and case.stage == "suspended"  # dunning takes it from here
    assert await trials.tick(ends + 30 * DAY) == []  # handed over once
    assert mails[-1]["subject"] == "Deine Apps sind angehalten"
    assert await trials.dunning.settle(maria.id, "in_1", time.time())  # the first paid invoice
    async with db.session() as session:
        app = await session.get(HostedApp, project_id)
    assert app is not None and not app.suspended


async def test_paying_ends_the_trial(server: LiveServer, mails: list[dict[str, Any]]) -> None:
    trials = trials_for(server)
    maria, web = await newcomer(server, "maria")
    await spend(server, maria.id, 3.0)
    assert await trials.refusal(maria) == OVER
    await subscribe(server, maria.id)
    state = await trials.state(maria)
    assert state is not None and state.status == "paid"
    assert await trials.refusal(maria) == ""
    assert await trials.tick(time.time()) == [(maria.id, "paid")]
    row = await trial_row(server, maria.id)
    assert row.ended_reason == "paid" and row.ended_at > 0
    assert await trials.tick(row.ends_at + 30 * DAY) == []  # no mail, no suspension
    assert mails == []
    assert (await web.get("/api/billing/me")).json()["entitlements"]["source"] == "plan:pro"


async def test_the_landing_page_shows_the_public_plans(server: LiveServer) -> None:
    now = time.time()
    async with server.services.db.session() as session, session.begin():
        session.add(CatalogItem(id="pro", kind="plan", name="Pro", amounts='{"month": 2900}',
                                limits=json.dumps(PRO), stripe_product="prod_secret",
                                stripe_prices='{"month": {"id": "price_1"}}', created_at=now,
                                updated_at=now))  # fmt: skip
        session.add(
            CatalogItem(
                id="draft",
                kind="plan",
                name="Draft",
                amounts='{"month": 100}',
                limits="{}",
                created_at=now,
                updated_at=now,
            )
        )  # not at Stripe
        session.add(CatalogItem(id="mac", kind="addon", name="Mac", amounts='{"month": 500}',
                                limits="{}", stripe_prices='{"month": {"id": "price_2"}}',
                                created_at=now, updated_at=now))  # fmt: skip
    async with WebClient(server) as visitor:  # nobody signed in
        found = await visitor.get("/api/public/plans")
        assert found.status_code == 200
        body = found.json()
        assert [p["id"] for p in body["plans"]] == ["pro"]
        assert body["plans"][0]["amounts"] == {"month": 2900}
        assert "prod_secret" not in found.text and "price_1" not in found.text
        assert body["trial"] == {"days": 10, "credit_usd": 3}
        assert found.headers.get("set-cookie") is None  # no cookies for visitors
        server.services.settings.billing.enabled = False
        assert (await visitor.get("/api/public/plans")).json() == {"plans": [], "trial": None}
        server.services.settings.hosting.provider_name = "Schmiede GmbH"
        imprint = await visitor.get("/impressum")
        assert imprint.status_code == 200 and "Schmiede GmbH" in imprint.text


async def test_the_guided_start_is_shown_once(server: LiveServer) -> None:
    _, web = await newcomer(server, "maria")
    assert (await web.get("/api/me")).json()["onboarding"] is True
    assert (await web.post("/api/me/onboarding", {})).status_code == 200
    assert (await web.get("/api/me")).json()["onboarding"] is False
    old = await person(server, "olga")  # accounts from before W72 skip it
    assert (await old.web.get("/api/me")).json()["onboarding"] is False
