"""The mail relay of hosted apps (W28): each release gets a token of its own, sealed like a secret;
it sends only within the app's mails a day, always from the server's address under the app's
name with the operator's address to reply to, never as anyone else, and never for a suspended
app. SMTP itself is a recorder here."""

from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from sqlalchemy import select

from forge_web.db.hosting_models import AppMailLog, HostedApp
from forge_web.hosting import mail_relay
from host_standin import HostStandIn, host_online
from support import LiveServer
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
RELAY = "/api/relay/mail"
MAIL = {"to": "kim@example.org", "subject": "Your sign-in link", "text": "Open this link: ..."}


@pytest.fixture
def sent(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """The mails the relay handed to SMTP."""
    mails: list[dict[str, Any]] = []

    async def record(smtp: Any, to: str, subject: str, body: str, **headers: str) -> None:
        mails.append({"to": to, "subject": subject, "body": body, **headers})

    monkeypatch.setattr(mail_relay, "send_mail", record)
    return mails


@pytest.fixture
def server(tmp_path: Path) -> Iterator[LiveServer]:
    settings = settings_for(tmp_path / "data")
    settings.auth.smtp.host, settings.auth.smtp.from_address = (
        "smtp.example.com",
        "apps@example.com",
    )
    settings.hosting.mail_daily_max = 2  # the template asks for 200 a day; the server allows 2
    with LiveServer(settings) as live:
        yield live


@pytest.fixture
async def client(server: LiveServer) -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(base_url=server.url, headers=server.headers(), timeout=60) as c:
        yield c


async def released(server: LiveServer, client: httpx.AsyncClient) -> tuple[str, str, HostStandIn]:
    """An app live in staging: its project id and the relay token its release opened."""
    host = HostStandIn()
    project_id, _ = await app_project(server, client, server.services.dev_user_id)
    async with host_online(server.url, await host_token(server, host), host):
        deploy_id = await deployed(client, project_id, "staging")
        assert (await until(lambda: settled(client, project_id, deploy_id)))["status"] == "live"
    release = host.last("release")
    return project_id, host.secrets[release.id]["MAIL_RELAY_TOKEN"], host


def bearer(token: str) -> dict[str, str]:
    """The relay's header for a token (none at all for an empty one)."""
    return {"Authorization": f"Bearer {token}"} if token else {}


async def test_a_token_sends_within_its_quota_and_only_as_its_app(
    server: LiveServer, client: httpx.AsyncClient, sent: list[dict[str, Any]]
) -> None:
    project_id, token, host = await released(server, client)
    plan = host.last("release").plan
    assert plan is not None
    api = plan.services[0]
    assert api.env["MAIL_RELAY_URL"].endswith(RELAY) and "MAIL_RELAY_TOKEN" in api.secrets
    assert "MAIL_RELAY_TOKEN" not in plan.services[1].secrets  # the static client gets none
    async with httpx.AsyncClient(base_url=server.url, timeout=30) as app:
        for _ in range(2):
            assert (await app.post(RELAY, json=MAIL, headers=bearer(token))).status_code == 200
        over = await app.post(RELAY, json=MAIL, headers=bearer(token))
    assert over.status_code == 429 and "2 mails for today" in over.text
    assert [m["to"] for m in sent] == ["kim@example.org"] * 2
    assert sent[0]["from_name"] == "shop" and sent[0]["reply_to"] == "ada@example.com"
    async with server.services.db.session() as session:
        kept = list(await session.scalars(select(AppMailLog)))
    assert [row.domain for row in kept] == ["example.org"] * 2  # no address, no text kept
    assert project_id


async def test_only_its_own_token_works_and_mails_cannot_smuggle_headers(
    server: LiveServer, client: httpx.AsyncClient, sent: list[dict[str, Any]]
) -> None:
    project_id, token, _ = await released(server, client)
    _, project, environment, secret = token.split("_", 3)
    forged = [f"fmr_{project}_production_{secret}", f"fmr_{project}_{environment}_x{secret}",
              token.replace("fmr_", "fhw_"), ""]  # fmt: skip
    async with httpx.AsyncClient(base_url=server.url, timeout=30) as app:
        for wrong in forged:
            assert (await app.post(RELAY, json=MAIL, headers=bearer(wrong))).status_code == 401
        smuggled = [
            {**MAIL, "subject": "Hi\r\nBcc: everyone@example.org"},
            {**MAIL, "to": "a@example.org, b@example.org"},
            {**MAIL, "to": "Kim <kim@example.org>"},
            {**MAIL, "from": "ceo@bank.example"},
        ]
        for body in smuggled:
            assert (await app.post(RELAY, json=body, headers=bearer(token))).status_code == 422
        async with server.services.db.session() as session, session.begin():
            hosted = await session.get(HostedApp, project_id)
            assert hosted is not None
            hosted.suspended = True
        stopped = await app.post(RELAY, json=MAIL, headers=bearer(token))
    assert stopped.status_code == 403
    assert sent == []
    reserved = await client.put(f"/api/projects/{project_id}/hosting/secrets",
                                json={"environment": "staging", "name": "MAIL_RELAY_TOKEN",
                                      "value": "mine"})  # fmt: skip
    assert reserved.status_code == 422


async def test_without_smtp_the_relay_says_so(
    server: LiveServer, client: httpx.AsyncClient
) -> None:
    _, token, _ = await released(server, client)
    server.services.settings.auth.smtp.host = ""
    async with httpx.AsyncClient(base_url=server.url, timeout=30) as app:
        answer = await app.post(RELAY, json=MAIL, headers=bearer(token))
    assert answer.status_code == 503 and "no SMTP" in answer.text
