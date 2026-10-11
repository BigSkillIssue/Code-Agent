"""Entitlements (W33a1): one engine sets every limit. Without billing (and for customers without a
plan) the server's own settings apply as before; a plan sets projects, the AI budget per month and
per product, Mac minutes, hosted apps, storage, own domains and store publishing, and add-ons and
hosting add to it; every place that enforces a limit asks the engine, and the gateway stops a
product at its own budget while the user's other products go on."""

import json
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi import HTTPException

from forge_web.apple.release_api import may_release
from forge_web.billing.entitlements import Entitlements
from forge_web.db.billing_models import CatalogItem, Subscription
from forge_web.db.models import Chat, Project, UsageRecord, User
from forge_web.gateway.keys import save_key
from forge_web.hosting.deploy_api import room_for_an_app
from forge_web.hosting.domains import room_for_a_domain
from forge_web.quotas import MB, check_project_count, disk_limit
from support import LiveServer, dev_settings, person
from test_gateway import call, token, world

__all__ = ["world"]
PRO = {"projects": 2, "ai_budget_month_eur": 50, "ai_budget_per_product_eur": 20,
       "mac_minutes": 100, "hosted_apps": 1, "storage_gb": 5, "custom_domains": 1,
       "store_publishing": True}  # fmt: skip


@pytest.fixture
def server(tmp_path: Path) -> Iterator[LiveServer]:
    settings = dev_settings(tmp_path / "data")
    settings.billing.enabled = True
    with LiveServer(settings) as live:
        yield live


async def item(server: LiveServer, item_id: str, kind: str, limits: dict[str, Any]) -> None:
    now = time.time()
    async with server.services.db.session() as session, session.begin():
        session.add(CatalogItem(id=item_id, kind=kind, name=item_id, amounts='{"month": 900}',
                                limits=json.dumps(limits), created_at=now,
                                updated_at=now))  # fmt: skip


async def subscribe(server: LiveServer, user_id: str, item_id: str, status: str = "active",
                    quantity: int = 1) -> None:  # fmt: skip
    now = time.time()
    async with server.services.db.session() as session, session.begin():
        session.add(Subscription(id=f"si_{item_id}_{user_id}", user_id=user_id, item_id=item_id,
                                 quantity=quantity, status=status, created_at=now,
                                 updated_at=now))  # fmt: skip


async def account(server: LiveServer, user_id: str) -> User:
    async with server.services.db.session() as session:
        found = await session.get(User, user_id)
    assert found is not None
    return found


async def project_of(server: LiveServer, owner: str) -> str:
    project_id = f"p{time.time_ns()}"
    now = time.time()
    async with server.services.db.session() as session, session.begin():
        session.add(Project(id=project_id, name="Shop", owner_id=owner, created_at=now,
                            updated_at=now))  # fmt: skip
    return project_id


async def test_without_a_plan_the_servers_own_limits_apply(server: LiveServer) -> None:
    settings, engine = server.services.settings, server.services.entitlements
    maria = await account(server, (await person(server, "maria")).id)
    found = await engine.of(maria)
    assert found == Entitlements(
        source="server", projects=settings.quotas.projects_per_user, ai_month_usd=None,
        server_keys=False,  # no grant for the server's keys
        mac_minutes=settings.apple.minutes_per_month, storage_mb=settings.quotas.project_disk_mb,
    )  # fmt: skip
    admin = await account(server, server.services.dev_user_id)
    assert (await engine.of(admin)).projects is None  # admins: any number of projects
    settings.billing.enabled = False
    await item(server, "pro", "plan", PRO)
    await subscribe(server, maria.id, "pro")
    assert (await engine.of(maria)).source == "server"  # billing off: plans count for nothing


async def test_a_plan_and_its_add_ons_set_every_limit(server: LiveServer) -> None:
    engine = server.services.entitlements
    maria = await account(server, (await person(server, "maria")).id)
    await item(server, "pro", "plan", PRO)
    await item(server, "mac", "addon", {"mac_minutes": 100})
    await item(server, "hosting", "hosting", {})
    await subscribe(server, maria.id, "pro")
    await subscribe(server, maria.id, "mac", quantity=2)
    await subscribe(server, maria.id, "hosting", quantity=2)
    rate = server.services.settings.billing.usd_per_eur
    assert await engine.of(maria) == Entitlements(
        source="plan:pro", projects=2, ai_month_usd=50 * rate, ai_product_usd=20 * rate,
        server_keys=True, mac_minutes=300, hosted_apps=3, storage_mb=5000, custom_domains=1,
        store_publishing=True,
    )  # fmt: skip
    async with server.services.db.session() as session, session.begin():
        cancelled = await session.get(Subscription, f"si_pro_{maria.id}")
        assert cancelled is not None
        cancelled.status = "canceled"
    assert (await engine.of(maria)).source == "server"  # without the plan, add-ons give nothing
    await item(server, "free", "plan", {"projects": 1, "store_publishing": False})
    server.services.settings.billing.free_plan = "free"
    free = await engine.of(maria)
    assert (free.source, free.projects, free.store_publishing, free.ai_month_usd) == (
        "plan:free", 1, False, None)  # fmt: skip


async def test_every_limit_is_enforced_from_the_engine(server: LiveServer) -> None:
    services = server.services
    small = {"projects": 1, "hosted_apps": 0, "custom_domains": 0, "storage_gb": 0.5,
             "mac_minutes": 0, "store_publishing": False}  # fmt: skip
    await item(server, "small", "plan", small)
    maria = await account(server, (await person(server, "maria")).id)
    await subscribe(server, maria.id, "small")
    project_id = await project_of(server, maria.id)
    with pytest.raises(HTTPException) as projects:
        await check_project_count(services, maria)
    assert projects.value.status_code == 403
    assert await disk_limit(services, project_id) == 500 * MB
    with pytest.raises(HTTPException) as hosted:
        await room_for_an_app(services, maria.id, project_id)
    assert "0 hosted apps" in str(hosted.value.detail)
    with pytest.raises(HTTPException) as domains:
        await room_for_a_domain(services, project_id)
    assert "0 own domains" in str(domains.value.detail)
    services.settings.apple.enabled = True
    services.settings.apple.allowed = "everyone"
    with pytest.raises(HTTPException) as store:
        await may_release(services, maria, project_id)
    assert "publishing to the stores" in str(store.value.detail)
    big = {"projects": 5, "hosted_apps": 2, "custom_domains": 3, "store_publishing": True}
    await item(server, "big", "plan", big)
    await subscribe(server, maria.id, "big")  # the newest plan counts
    await check_project_count(services, maria)
    await room_for_an_app(services, maria.id, project_id)
    await room_for_a_domain(services, project_id)
    assert await disk_limit(services, project_id) is None  # no storage limit in this plan


async def test_the_gateway_stops_a_product_at_its_own_budget(world: dict[str, Any]) -> None:
    db, gateway = world["db"], world["gateway"]
    now = time.time()
    async with db.session() as session, session.begin():
        session.add(Project(id="p2", name="Q", owner_id="u1", created_at=now, updated_at=now))
        await session.flush()
        session.add(Chat(id="c3", project_id="p2", user_id="u1", created_at=now, updated_at=now))
        session.add(UsageRecord(user_id="u1", chat_id="c1", project_id="p1", provider="anthropic",
                                key_kind="server", cost_usd=0.995, created_at=now))  # fmt: skip
    world["working"].add("c3")
    await save_key(db, world["vault"], "", "anthropic", "sk-ant-server-key-0001")
    limits = Entitlements(source="plan:pro", ai_month_usd=10.0, ai_product_usd=1.0)

    async def plan(user: User) -> Entitlements:
        return limits

    gateway.limits_of = plan
    stopped = await call(world, token(world))  # p1 spent 0.995 of its 1.00 this month
    assert stopped.status_code == 403
    assert "product's monthly AI budget" in stopped.json()["error"]["message"]
    assert (await call(world, token(world, "c3"))).status_code == 200  # p2 has its own
    limits = Entitlements(source="server", server_keys=False)
    assert (await call(world, token(world, "c3"))).status_code == 403  # no server keys at all
