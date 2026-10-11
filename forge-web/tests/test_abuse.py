"""Reports of illegal content (W28b, DSA Art. 16 and 17): the edge's report form takes a notice
and confirms it, the admins hear of it and decide; suspending the app stops it at once and sends
its creator a statement of reasons, dismissing tells the notifier why. SMTP is a recorder."""

import asyncio
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from sqlalchemy import select

from forge_web.db.hosting_models import AbuseNotice, HostedApp
from forge_web.db.models import User
from forge_web.hosting import abuse
from host_standin import HostStandIn, host_online
from support import LiveServer, person
from test_hosting_deploy import (
    app_project,
    deployed,
    host_token,
    settings_for,
    settled,
    short_polls,
    until,
)

__all__ = ["short_polls"]
FORM = {"category": "fraud", "location": "https://shop.staging.apps.example/offers/7",
        "reason": "This offer takes payment for goods that are never sent.",
        "name": "Kim Muster", "email": "kim@example.org", "good_faith": "yes"}  # fmt: skip


@pytest.fixture
def mails(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """The mails handed to SMTP."""
    sent: list[dict[str, Any]] = []

    async def record(smtp: Any, to: str, subject: str, body: str, **headers: str) -> None:
        sent.append({"to": to, "subject": subject, "body": body, **headers})

    monkeypatch.setattr(abuse, "send_mail", record)
    return sent


@pytest.fixture
def server(tmp_path: Path) -> Iterator[LiveServer]:
    settings = settings_for(tmp_path / "data")
    settings.auth.smtp.host, settings.auth.smtp.from_address = (
        "smtp.example.com",
        "apps@example.com",
    )
    settings.hosting.contact_email = "abuse@example.com"
    with LiveServer(settings) as live:
        yield live


@pytest.fixture
async def client(server: LiveServer) -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(base_url=server.url, headers=server.headers(), timeout=60) as c:
        yield c


async def live_app(
    server: LiveServer, web: Any, user_id: str, host: HostStandIn
) -> tuple[str, str]:
    """An app project live in staging: its id and its host's token."""
    project_id, _ = await app_project(server, web, user_id)
    token = await host_token(server, host)
    async with host_online(server.url, token, host):
        deploy_id = await deployed(web, project_id, "staging")
        assert (await until(lambda: settled(web, project_id, deploy_id)))["status"] == "live"
    return project_id, token


async def report(server: LiveServer, form: dict[str, str]) -> httpx.Response:
    """A visitor sends the form, as the edge forwards it (no sign-in)."""
    async with httpx.AsyncClient(base_url=server.url, timeout=30) as visitor:
        return await visitor.post("/legal/shop/melden", data=form)


async def until_mailed(mails: list[dict[str, Any]], count: int) -> None:
    async with asyncio.timeout(10):
        while len(mails) < count:
            await asyncio.sleep(0.05)


async def test_a_notice_reaches_the_admins_and_is_confirmed(
    server: LiveServer, client: httpx.AsyncClient, mails: list[dict[str, Any]]
) -> None:
    await live_app(server, client, server.services.dev_user_id, HostStandIn())
    async with httpx.AsyncClient(base_url=server.url, timeout=30) as visitor:
        shown = await visitor.get("/legal/shop/melden", params={"url": "https://x.example/<b>"})
        missing = await visitor.get("/legal/nobody/melden")
    assert shown.status_code == 200 and 'action="/melden"' in shown.text
    assert "https://x.example/&lt;b&gt;" in shown.text and "Art. 16" in shown.text
    assert missing.status_code == 404
    sent = await report(server, FORM)
    assert sent.status_code == 200 and "Danke" in sent.text
    [notice] = (await client.get("/api/admin/hosting/notices")).json()
    assert (notice["app"], notice["category"], notice["status"]) == ("shop", "fraud", "open")
    assert notice["location"] == FORM["location"] and notice["email"] == "kim@example.org"
    await until_mailed(mails, 2)
    by_to = {m["to"]: m for m in mails}
    async with server.services.db.session() as session:
        admin = await session.get(User, server.services.dev_user_id)
    assert admin is not None and admin.email
    assert by_to[admin.email]["subject"] == "Neue Meldung zu shop"  # every admin hears of it
    assert notice["id"] in by_to["kim@example.org"]["body"]  # the confirmation (Art. 16(4))
    assert all(m["reply_to"] == "abuse@example.com" for m in mails)


async def test_an_incomplete_notice_gets_the_form_again(
    server: LiveServer, client: httpx.AsyncClient, mails: list[dict[str, Any]]
) -> None:
    await live_app(server, client, server.services.dev_user_id, HostStandIn())
    lacking = await report(server, {**FORM, "good_faith": "", "name": "", "reason": "bad"})
    assert lacking.status_code == 400
    assert (
        "gutem Glauben" in lacking.text and "Namen" in lacking.text and "begründen" in lacking.text
    )
    assert FORM["location"] in lacking.text  # what was typed stays
    anonymous = await report(server, {**FORM, "category": "child_abuse", "name": "", "email": ""})
    assert anonymous.status_code == 200  # no name needed for child sexual abuse (Art. 16(2)(c))
    rows = (await client.get("/api/admin/hosting/notices")).json()
    assert [r["category"] for r in rows] == ["child_abuse"]


async def test_suspending_stops_the_app_and_sends_the_creator_its_reasons(
    server: LiveServer, client: httpx.AsyncClient, mails: list[dict[str, Any]]
) -> None:
    host = HostStandIn()
    maria = await person(server, "maria")
    project_id, token = await live_app(server, maria.web, maria.id, host)
    await report(server, FORM)
    [notice] = (await client.get("/api/admin/hosting/notices")).json()
    await until_mailed(mails, 2)
    mails.clear()
    decision = {"action": "suspend", "reason": "The offers are fraudulent; payments are taken."}
    async with host_online(server.url, token, host):  # the app's own host gets the stop
        decided = await client.post(f"/api/admin/hosting/notices/{notice['id']}/decide",
                                    json=decision)  # fmt: skip
        assert decided.status_code == 200 and decided.json()["status"] == "actioned"
        await until(lambda: asyncio.sleep(0, "stop" in host.kinds()))
    async with server.services.db.session() as session:
        hosted = await session.get(HostedApp, project_id)
        stored = await session.get(AbuseNotice, notice["id"])
    assert hosted is not None and hosted.suspended and notice["id"] in hosted.suspended_reason
    assert stored is not None and stored.decision == decision["reason"]
    await until_mailed(mails, 2)
    creator = next(m for m in mails if m["to"] == "maria@example.com")
    for part in ("gesperrt", "Art. 16", decision["reason"], FORM["reason"], "Art. 21 DSA",
                 "Mensch"):  # fmt: skip
        assert part in creator["body"], part
    notifier = next(m for m in mails if m["to"] == "kim@example.org")
    assert "gesperrt" in notifier["body"]
    again = await client.post(f"/api/admin/hosting/notices/{notice['id']}/decide", json=decision)
    assert again.status_code == 409
    refused = await maria.web.post(
        f"/api/projects/{project_id}/deploys", {"environment": "staging"}
    )
    assert refused.status_code == 409 and "suspended" in refused.text
    await maria.web.client.aclose()


async def test_dismissing_tells_the_notifier_why(
    server: LiveServer, client: httpx.AsyncClient, mails: list[dict[str, Any]]
) -> None:
    project_id, _ = await live_app(server, client, server.services.dev_user_id, HostStandIn())
    await report(server, FORM)
    [notice] = (await client.get("/api/admin/hosting/notices")).json()
    await until_mailed(mails, 2)
    mails.clear()
    body = {"action": "dismiss", "reason": "The offer is a real shop; goods were sent."}
    decided = await client.post(f"/api/admin/hosting/notices/{notice['id']}/decide", json=body)
    assert decided.json()["status"] == "dismissed"
    await until_mailed(mails, 1)
    assert mails[0]["to"] == "kim@example.org" and body["reason"] in mails[0]["body"]
    assert "keine Maßnahme" in mails[0]["body"]
    async with server.services.db.session() as session:
        hosted = await session.get(HostedApp, project_id)
        rows = list(await session.scalars(select(AbuseNotice)))
    assert hosted is not None and not hosted.suspended
    assert [r.status for r in rows] == ["dismissed"]


async def test_many_notices_from_one_address_are_slowed_down(
    server: LiveServer, client: httpx.AsyncClient, mails: list[dict[str, Any]]
) -> None:
    await live_app(server, client, server.services.dev_user_id, HostStandIn())
    for _ in range(abuse.PER_HOUR):
        assert (await report(server, FORM)).status_code == 200
    assert (await report(server, FORM)).status_code == 429
