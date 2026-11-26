"""SEC-W4: the public report form cannot be used to send mail: the confirmation carries no text
of the sender's, confirmations to one address and notices from one address across apps are
limited, and the admins get one mail per while that sums up what came in."""

import asyncio
import secrets
import time
from typing import Any

import httpx
from sqlalchemy import func, select

from forge_web.auth.ratelimit import RateLimiter
from forge_web.db.hosting_models import AbuseNotice, HostedApp
from forge_web.db.models import Project, User
from host_standin import HostStandIn
from support import LiveServer
from test_abuse import FORM, client, live_app, mails, report, server, until_mailed
from test_hosting_deploy import short_polls

__all__ = ["client", "mails", "server", "short_polls"]
PHISH = "https://evil.example/login Ihr Konto wird gesperrt, bitte sofort anmelden"
ANONYMOUS = {**FORM, "category": "child_abuse", "name": "", "email": ""}  # no confirmation
REASON = "Bitte klicken Sie https://evil.example/pay und zahlen Sie die Gebühr."


async def admin_email(server: LiveServer) -> str:
    async with server.services.db.session() as session:
        admin = await session.get(User, server.services.dev_user_id)
    assert admin is not None and admin.email
    return admin.email


async def notices(server: LiveServer) -> int:
    async with server.services.db.session() as session:
        return int(await session.scalar(select(func.count(AbuseNotice.id))) or 0)


async def second_app(server: LiveServer, name: str) -> None:
    """Another hosted app on the same host (only its name matters to the form)."""
    now = time.time()
    async with server.services.db.session() as session, session.begin():
        shop = await session.scalar(select(HostedApp).where(HostedApp.app == "shop"))
        assert shop is not None
        pid = secrets.token_hex(8)
        session.add(Project(id=pid, name=name, owner_id=server.services.dev_user_id,
                            created_at=now, updated_at=now))  # fmt: skip
        await session.flush()
        session.add(HostedApp(project_id=pid, app=name, host_id=shop.host_id, created_at=now))


async def test_the_mails_carry_no_text_of_the_sender(
    server: LiveServer, client: httpx.AsyncClient, mails: list[dict[str, Any]]
) -> None:
    await live_app(server, client, server.services.dev_user_id, HostStandIn())
    sent = await report(server, {**FORM, "location": PHISH, "reason": REASON,
                                 "name": "Bank Support https://evil.example"})  # fmt: skip
    assert sent.status_code == 200
    [row] = (await client.get("/api/admin/hosting/notices")).json()
    assert row["location"] == PHISH and row["reason"] == REASON  # the notice is kept as sent
    await until_mailed(mails, 2)
    await asyncio.sleep(0.2)
    for mail in mails:
        assert "evil.example" not in mail["body"] + mail["subject"], mail
    confirmation = next(m for m in mails if m["to"] == FORM["email"])
    assert row["id"] in confirmation["body"] and "eingegangen" in confirmation["body"]


async def test_confirmations_to_one_address_are_limited(
    server: LiveServer, client: httpx.AsyncClient, mails: list[dict[str, Any]]
) -> None:
    await live_app(server, client, server.services.dev_user_id, HostStandIn())
    for _ in range(8):
        sent = await report(server, {**FORM, "email": "Victim@Example.org"})
        assert sent.status_code == 200 and "Nummer" in sent.text  # the page confirms each one
    await asyncio.sleep(0.5)
    assert await notices(server) == 8  # every notice is recorded
    to_victim = [m for m in mails if m["to"].lower() == "victim@example.org"]
    assert 1 <= len(to_victim) <= 5, len(to_victim)


async def test_one_address_is_limited_across_app_names(
    server: LiveServer, client: httpx.AsyncClient, mails: list[dict[str, Any]]
) -> None:
    await live_app(server, client, server.services.dev_user_id, HostStandIn())
    await second_app(server, "shop2")
    server.services.limits.notices_by_address = RateLimiter(3, 3600)  # small, for the test
    async with httpx.AsyncClient(base_url=server.url, timeout=30) as visitor:
        codes = [(await visitor.post(f"/legal/{app}/melden", data=FORM)).status_code
                 for app in ("shop", "shop2", "shop", "shop2")]  # fmt: skip
    assert codes == [200, 200, 200, 429], codes
    assert await notices(server) == 3


async def test_admins_get_one_mail_per_while(
    server: LiveServer, client: httpx.AsyncClient, mails: list[dict[str, Any]]
) -> None:
    await live_app(server, client, server.services.dev_user_id, HostStandIn())
    admin = await admin_email(server)
    for _ in range(3):
        assert (await report(server, ANONYMOUS)).status_code == 200
    await until_mailed(mails, 1)
    await asyncio.sleep(0.5)
    to_admin = [m for m in mails if m["to"] == admin]
    assert len(to_admin) == 1 and to_admin[0]["subject"] == "Neue Meldung zu shop"

    # the next mail sums up what came in meanwhile, once the while is over
    server.services.notice_mail.window = 0.0
    await until_mailed(mails, 2)
    digest = [m for m in mails if m["to"] == admin][-1]
    assert digest["subject"] == "2 neue Meldungen", digest
    assert digest["body"].count("shop:") == 2 and "/admin" in digest["body"]
