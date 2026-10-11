"""The go-live page's data and the admin's hosting panel (W27): the creator sees the reviews, the
checks and the staging link and can go live only after staging is healthy; the admin's queue
shows exactly the commit the host checked; trusting a user spares their next deploy the admin;
a suspended app is stopped (and so taken off the edge) and gets no deploys; only admins reach
the panel."""

import json
import time
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from sqlalchemy import select

from forge_hostworker.wire import JobResult
from forge_web.db.hosting_models import HostServer
from forge_web.db.models import AuditEntry, Chat, ChatEvent
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

ADMIN = "/api/admin/hosting"
__all__ = ["short_polls"]  # the fixture that keeps long polls short applies here too


@pytest.fixture
def server(tmp_path: Path) -> Iterator[LiveServer]:
    with LiveServer(settings_for(tmp_path / "data")) as live:
        yield live


@pytest.fixture
async def client(server: LiveServer) -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(base_url=server.url, headers=server.headers(), timeout=60) as c:
        yield c


async def reviewed(server: LiveServer, project_id: str, user_id: str) -> None:
    """A chat of the project with the release reviewer's verdict on the finished product."""
    now = time.time()
    event = {
        "kind": "release_review",
        "stage": "product",
        "verdict": "ok",
        "summary": "Ready: privacy page, Impressum and reporting are there.",
        "findings": [],
    }
    async with server.services.db.session() as session, session.begin():
        session.add(Chat(id="c-review", project_id=project_id, user_id=user_id, created_at=now,
                         updated_at=now))  # fmt: skip
        await session.flush()
        data = json.dumps({"event": event})
        session.add(
            ChatEvent(
                chat_id="c-review", seq=1, ts=now, type="event", kind="release_review", data=data
            )
        )


async def overview(web: Any, project_id: str) -> dict[str, Any]:
    return dict((await web.get(f"/api/projects/{project_id}/hosting")).json())


async def test_the_creator_sees_reviews_checks_and_the_staging_link(
    server: LiveServer, client: httpx.AsyncClient
) -> None:
    host = HostStandIn()
    project_id, head = await app_project(server, client, server.services.dev_user_id)
    await reviewed(server, project_id, server.services.dev_user_id)
    before = await overview(client, project_id)
    assert before["enabled"] and before["staging"] is None and before["production"] is None
    assert [a["commit"] for a in before["approvals"]] == [head]
    assert [(r["stage"], r["verdict"]) for r in before["reviews"]] == [("product", "ok")]
    url = f"/api/projects/{project_id}/deploys"
    early = await client.post(url, json={"environment": "production"})
    assert early.status_code == 409  # not before staging is healthy
    async with host_online(server.url, await host_token(server, host), host):
        staging = await deployed(client, project_id, "staging")
        await until(lambda: settled(client, project_id, staging))
        shown = await overview(client, project_id)
        assert shown["app"] == "shop"
        assert shown["staging"]["url"] == "https://shop.staging.apps.example"
        assert {c["name"] for c in shown["staging"]["checks"]} >= {"api: tests", "web: tests"}
        production = await deployed(client, project_id, "production")
        live = await until(lambda: settled(client, project_id, production))
    assert live["status"] == "live"
    assert (await overview(client, project_id))["production"]["url"] == "https://shop.apps.example"


async def test_the_queue_shows_exactly_the_checked_commit(
    server: LiveServer, client: httpx.AsyncClient
) -> None:
    host = HostStandIn()
    maria = await person(server, "maria")
    project_id, head = await app_project(server, maria.web, maria.id)
    async with host_online(server.url, await host_token(server, host), host):
        staging = await deployed(maria.web, project_id, "staging")
        staged = await until(lambda: settled(maria.web, project_id, staging))
        assert (await client.get(f"{ADMIN}/queue")).json() == []
        production = await deployed(maria.web, project_id, "production")
        await until(lambda: settled(maria.web, project_id, production))
        [item] = (await client.get(f"{ADMIN}/queue")).json()
        assert (item["id"], item["commit"], item["project"]) == (production, head, "Shop")
        assert item["checks"] == staged["checks"] and item["checks_from"] == staging
        assert item["creator"] == "Maria"
        reason = {"reason": "the privacy page names no contact"}
        refused = await client.post(f"{ADMIN}/deploys/{production}/refuse", json=reason)
        assert refused.status_code == 200
        gone = await settled(maria.web, project_id, production)
        assert gone is not None and gone["status"] == "failed"
        assert "admin refused it: the privacy page names no contact" in gone["error"]
        assert (await client.get(f"{ADMIN}/queue")).json() == []
        assert host.kinds("production") == []  # nothing ran
    await maria.web.client.aclose()


async def test_trusting_a_user_spares_their_next_deploy_the_admin(
    server: LiveServer, client: httpx.AsyncClient
) -> None:
    host = HostStandIn()
    maria = await person(server, "maria")
    project_id, _ = await app_project(server, maria.web, maria.id)
    users = (await client.get(f"{ADMIN}/users")).json()
    assert {u["email"]: u["trusted"] for u in users}["maria@example.com"] is False
    assert (await maria.web.request("PUT", f"{ADMIN}/users/{maria.id}",
                                    json={"trusted": True})).status_code == 403  # fmt: skip
    trusted = await client.put(f"{ADMIN}/users/{maria.id}", json={"trusted": True})
    assert trusted.json() == {"trusted": True}
    async with host_online(server.url, await host_token(server, host), host):
        staging = await deployed(maria.web, project_id, "staging")
        await until(lambda: settled(maria.web, project_id, staging))
        production = await deployed(maria.web, project_id, "production")
        live = await until(lambda: settled(maria.web, project_id, production))
    assert live["status"] == "live" and live["admin"] == "trusted"
    await maria.web.client.aclose()


async def test_a_suspended_app_is_stopped_and_gets_no_deploys(
    server: LiveServer, client: httpx.AsyncClient
) -> None:
    host = HostStandIn()
    host.results["logs"] = JobResult(ok=True, log_tail="--- api\nGET /api/healthz 200")
    project_id, _ = await app_project(server, client, server.services.dev_user_id)
    async with host_online(server.url, await host_token(server, host), host):
        staging = await deployed(client, project_id, "staging")
        await until(lambda: settled(client, project_id, staging))
        [app] = (await client.get(f"{ADMIN}/apps")).json()
        assert app["app"] == "shop" and app["live"]["staging"]["release"] == 1
        logs = await client.post(f"{ADMIN}/apps/{project_id}/logs", json={"environment": "staging"})
        assert "GET /api/healthz 200" in logs.json()["log_tail"]
        stop = {"suspended": True, "reason": "a notice under the DSA is being checked"}
        assert (await client.post(f"{ADMIN}/apps/{project_id}/suspend", json=stop)).json() == {
            "suspended": True, "environments": ["staging"]}  # fmt: skip
        await until(lambda: async_true("stop" in host.kinds()))
        shown = await overview(client, project_id)
        assert shown["suspended"] and "DSA" in shown["suspended_reason"]
        again = await client.post(f"/api/projects/{project_id}/deploys",
                                  json={"environment": "staging"})  # fmt: skip
        assert again.status_code == 409 and "suspended" in again.text
        resume = await client.post(f"{ADMIN}/apps/{project_id}/suspend",
                                   json={"suspended": False})  # fmt: skip
        assert resume.status_code == 200
        await until(lambda: async_true("start" in host.kinds()))
    assert host.kinds() == ["check", "migrate", "release", "logs", "stop", "start"]
    async with server.services.db.session() as session:
        actions = [a.action for a in await session.scalars(select(AuditEntry))]
    assert {"hosting.app_suspended", "hosting.app_resumed"} <= set(actions)


async def async_true(value: bool) -> bool:
    return value


async def test_hosts_get_a_token_once_and_a_disabled_host_is_refused(
    server: LiveServer, client: httpx.AsyncClient
) -> None:
    host = HostStandIn()
    bad = await client.post(f"{ADMIN}/hosts", json={"name": "h", "public_key": "x" * 44})
    assert bad.status_code == 422
    new = {"name": "host-1", "public_key": host.public_key}
    made = (await client.post(f"{ADMIN}/hosts", json=new)).json()
    assert made["token"].startswith("fhw_") and made["online"] is False
    listed = (await client.get(f"{ADMIN}/hosts")).json()
    assert [h["name"] for h in listed] == ["host-1"] and "token" not in listed[0]
    poll = {"Authorization": f"Bearer {made['token']}"}
    async with httpx.AsyncClient(base_url=server.url, timeout=30) as hosted:
        assert (await hosted.post("/api/host/poll", json={"free_slots": 1},
                                  headers=poll)).status_code == 200  # fmt: skip
        off = await client.patch(f"{ADMIN}/hosts/{made['id']}", json={"enabled": False})
        assert off.json()["enabled"] is False
        refused = await hosted.post("/api/host/poll", json={"free_slots": 1}, headers=poll)
    assert refused.status_code == 401
    async with server.services.db.session() as session:
        stored = await session.get(HostServer, made["id"])
    assert stored is not None and made["token"].split("_", 2)[2] not in stored.token_hash


async def test_only_admins_reach_the_panel(server: LiveServer) -> None:
    maria = await person(server, "maria")
    for path in ("/hosts", "/queue", "/apps", "/users"):
        assert (await maria.web.get(f"{ADMIN}{path}")).status_code == 403
    await maria.web.client.aclose()
