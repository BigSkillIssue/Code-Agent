"""Dunning (W33b2): an unpaid bill opens a case; after the grace period the customer's hosted
apps are suspended, later they are exported and the deletion is announced, and only after that
date, with the export there, are they deleted; a paid bill closes the case and lets the apps run
again. Every stage comes at its date. The host is the stand-in host."""

import asyncio
import time
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from sqlalchemy import select

from forge_web.billing import dunning as dunning_module
from forge_web.billing.dunning import DAY, UNPAID, Dunning
from forge_web.db.billing_models import BillingCustomer, DunningCase
from forge_web.db.hosting_models import AppBackup, HostedApp
from forge_web.hosting import hosts as host_queue
from forge_web.hosting.hosts import HostQueue
from forge_web.hosting.ops import Backups
from forge_web.hosting.removal import Removals
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


@pytest.fixture
def mails(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    sent: list[dict[str, Any]] = []

    async def record(smtp: Any, to: str, subject: str, text: str, **headers: str) -> None:
        sent.append({"to": to, "subject": subject, "body": text})

    monkeypatch.setattr(dunning_module, "send_mail", record)
    monkeypatch.setattr(host_queue, "RECHECK_S", 0.2)
    return sent


@pytest.fixture
def server(tmp_path: Path) -> Iterator[LiveServer]:
    settings = settings_for(tmp_path / "data")
    settings.billing.enabled = True
    settings.auth.smtp.host, settings.auth.smtp.from_address = "smtp.example", "pay@example.com"
    with LiveServer(settings) as live:
        yield live


@pytest.fixture
async def client(server: LiveServer) -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(base_url=server.url, headers=server.headers(), timeout=60) as c:
        yield c


def dunning_for(server: LiveServer) -> Dunning:
    """Dunning of its own on this test's loop, on the server's database and files."""
    services = server.services
    queue = HostQueue(services.db, services.settings.data_dir, 600)
    backups = Backups(services.db, services.vault, services.settings, queue)
    return Dunning(services.db, services.settings, queue, backups,
                   Removals(services.db, services.settings, queue))  # fmt: skip


async def hosted(server: LiveServer, project_id: str) -> HostedApp | None:
    async with server.services.db.session() as session:
        return await session.get(HostedApp, project_id)


async def backed_up(server: LiveServer, project_id: str) -> bool:
    async with server.services.db.session() as session:
        return await session.scalar(select(AppBackup.id).where(
            AppBackup.project_id == project_id)) is not None  # fmt: skip


async def test_every_dunning_stage_comes_at_its_date(
    server: LiveServer, client: httpx.AsyncClient, mails: list[dict[str, Any]]
) -> None:
    host = HostStandIn()
    owner = server.services.dev_user_id
    project_id, _ = await app_project(server, client, owner)
    dunning = dunning_for(server)
    async with host_online(server.url, await host_token(server, host), host):
        deploy_id = await deployed(client, project_id, "staging")
        await until(lambda: settled(client, project_id, deploy_id))
        start = time.time() - 60 * DAY  # the dates lie behind us, so real backups come after them
        case = await dunning.open(owner, start)
        assert (await dunning.open(owner, start)).id == case.id  # one case at a time
        assert await dunning.tick(start + 13.9 * DAY) == []
        assert await dunning.tick(start + 14 * DAY) == [(case.id, "suspended")]
        app = await hosted(server, project_id)
        assert app is not None and app.suspended and app.suspended_reason == UNPAID
        await until(lambda: has(host, "stop"))
        assert await dunning.tick(start + 27.9 * DAY) == []
        host.holds["archive"] = asyncio.Event()  # the export takes its time
        assert await dunning.tick(start + 28 * DAY) == [(case.id, "noticed")]
        await until(lambda: has(host, "archive"))
        assert await dunning.tick(start + 58 * DAY) == []  # no deletion before the export
        assert await hosted(server, project_id) is not None
        host.holds["archive"].set()
        await until(lambda: backed_up(server, project_id))
        assert await dunning.tick(start + 58 * DAY) == [(case.id, "deleted")]
        await until(lambda: gone(server, project_id))
    subjects = [m["subject"] for m in mails]
    assert subjects == ["Deine Zahlung ist fehlgeschlagen", "Deine Apps sind angehalten",
                        "Deine Apps werden gelöscht", "Deine Apps wurden gelöscht"]  # fmt: skip
    deletion_day = time.strftime("%d.%m.%Y", time.gmtime(start + 58 * DAY))
    assert deletion_day in mails[2]["body"]
    assert await dunning.tick(time.time()) == []  # the case is closed
    await dunning.close()


async def has(host: HostStandIn, kind: str) -> bool:
    return kind in host.kinds()


async def gone(server: LiveServer, project_id: str) -> bool:
    return await hosted(server, project_id) is None


async def test_a_paid_bill_lets_the_apps_run_again(
    server: LiveServer, client: httpx.AsyncClient, mails: list[dict[str, Any]]
) -> None:
    host = HostStandIn()
    owner = server.services.dev_user_id
    project_id, _ = await app_project(server, client, owner)
    dunning = dunning_for(server)
    assert {"invoice.paid", "invoice.payment_failed"} <= set(
        server.services.billing.events.handlers
    )
    async with server.services.db.session() as session, session.begin():
        session.add(BillingCustomer(user_id=owner, stripe_customer="cus_7", created_at=time.time()))
    async with host_online(server.url, await host_token(server, host), host):
        deploy_id = await deployed(client, project_id, "staging")
        await until(lambda: settled(client, project_id, deploy_id))
        start = time.time() - 20 * DAY
        await dunning.failed({"data": {"object": {"customer": "cus_7"}}})
        async with server.services.db.session() as session, session.begin():
            case = await session.scalar(select(DunningCase))
            assert case is not None
            case.opened_at = case.stage_at = start
        assert [stage for _, stage in await dunning.tick(time.time())] == ["suspended"]
        await dunning.paid({"data": {"object": {"customer": "cus_7"}}})
        app = await hosted(server, project_id)
        assert app is not None and not app.suspended
        await until(lambda: has(host, "start"))
        assert await dunning.tick(time.time() + 100 * DAY) == []  # paid: nothing follows
    await dunning.close()
