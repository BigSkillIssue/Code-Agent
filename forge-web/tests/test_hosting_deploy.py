"""Deploys (W26), end to end against a stand-in host: an approved commit goes to staging, is
checked by the host itself, then the same commit to production after the creator's and an
admin's approval. A deploy goes on after a restart without migrating twice, a release that does
not get healthy is rolled back with the reason, secrets are sealed to the host and handed out
once, and only a commit the user said is "Ready to go live" is deployed."""

import asyncio
import json
import subprocess
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from forge.app_flow import GO_LIVE, NOT_YET, SEND_BACK
from sqlalchemy import select

from forge_hostworker.wire import JobResult
from forge_web.db.hosting_models import AppSecret, GoLiveApproval, HostJobRow
from forge_web.db.models import AuditEntry, User
from forge_web.hosting import deploy_api
from forge_web.hosting.hosts import create_host
from forge_web.hosting.questions import is_go_live
from host_standin import HostStandIn, host_online
from support import Browser, LiveServer, Person, call, dev_settings, fake_script, free_port, person

DOMAIN = "apps.example"


def settings_for(data: Path) -> Any:
    settings = dev_settings(data)
    settings.hosting.enabled, settings.hosting.apps_domain = True, DOMAIN
    return settings


@pytest.fixture(autouse=True)
def short_polls(monkeypatch: pytest.MonkeyPatch) -> None:
    """Hosts ask again every second, so a server stops without waiting for a long poll."""
    monkeypatch.setattr(deploy_api, "POLL_WAIT_S", 1.0)


@pytest.fixture
def server(tmp_path: Path) -> Iterator[LiveServer]:
    with LiveServer(settings_for(tmp_path / "data")) as live:
        yield live


@pytest.fixture
async def client(server: LiveServer) -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(base_url=server.url, headers=server.headers(), timeout=60) as c:
        yield c


async def until(check: Callable[[], Awaitable[Any]], timeout: float = 60) -> Any:
    async with asyncio.timeout(timeout):
        while not (found := await check()):
            await asyncio.sleep(0.1)
        return found


def git(workspace: Path, *args: str) -> str:
    return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.com", *args],
                          cwd=workspace, capture_output=True, text=True,
                          check=True).stdout.strip()  # fmt: skip


async def app_project(server: LiveServer, web: Any, user_id: str) -> tuple[str, str]:
    """An app project whose HEAD its user said is "Ready to go live"."""
    made = await web.request("POST", "/api/projects", json={"name": "Shop", "source": "app"})
    assert made.status_code == 201, made.text
    project_id = str(made.json()["id"])
    return project_id, await approve_head(server, project_id, user_id)


async def approve_head(server: LiveServer, project_id: str, user_id: str) -> str:
    head = git(server.services.driver.workspace(project_id), "rev-parse", "HEAD")
    async with server.services.db.session() as session, session.begin():
        session.add(GoLiveApproval(id=f"g{time.time_ns()}", project_id=project_id, chat_id="c1",
                                   user_id=user_id, request_id="r1", commit=head, clean=True,
                                   created_at=time.time()))  # fmt: skip
    return head


async def host_token(server: LiveServer, host: HostStandIn) -> str:
    _, token = await create_host(server.services.db, "host-1", host.public_key)
    return token


async def deployed(web: Any, project_id: str, environment: str, commit: str = "") -> str:
    started = await web.request("POST", f"/api/projects/{project_id}/deploys",
                             json={"environment": environment, "commit": commit})  # fmt: skip
    assert started.status_code == 200, started.text
    return str(started.json()["id"])


async def settled(web: Any, project_id: str, deploy_id: str) -> dict[str, Any] | None:
    """The deploy once it no longer runs."""
    rows = (await web.get(f"/api/projects/{project_id}/deploys")).json()
    row = next(r for r in rows if r["id"] == deploy_id)
    return row if row["status"] != "running" else None


async def test_staging_then_production_end_to_end(
    server: LiveServer, client: httpx.AsyncClient
) -> None:
    host = HostStandIn()
    project_id, head = await app_project(server, client, server.services.dev_user_id)
    async with host_online(server.url, await host_token(server, host), host):
        staging = await deployed(client, project_id, "staging")
        first = await until(lambda: settled(client, project_id, staging))
        assert (first["status"], first["step"]) == ("live", "done"), first["error"]
        assert first["url"] == "https://shop.staging.apps.example" and first["commit"] == head
        assert {c["name"] for c in first["checks"]} == {"api: tests", "api: migrations",
                                                         "web: tests"}  # fmt: skip
        assert host.kinds() == ["check", "migrate", "release"]  # nothing live yet: no backup
        check = host.last("check")
        assert check.plan is not None
        api = check.plan.services[0]
        assert (api.test, api.migrate, api.check_build) == (
            ["pytest", "-q"],
            ["alembic", "upgrade", "head"],
            ["uv", "sync", "--locked"],
        )
        assert api.image == server.services.settings.hosting.images["python3.12"]
        assert api.env["APP_URL"] == "https://shop.staging.apps.example"
        assert check.plan.database and check.plan.storage_gb == 1
        files = host.sources[check.id]
        assert {"forge.app.toml", "server/alembic.ini", "web/package.json"} <= files
        production = await deployed(client, project_id, "production")
        live = await until(lambda: settled(client, project_id, production))
        assert live["status"] == "live", live["error"]
        assert live["url"] == "https://shop.apps.example" and live["checks_from"] == staging
        assert live["admin"] == "admin"  # the creator is an admin: no second admin needed
        assert host.kinds("production") == ["migrate", "release"]  # checked once, in staging
        again = await deployed(client, project_id, "staging")
        await until(lambda: settled(client, project_id, again))
        assert host.kinds("staging")[-4:] == ["check", "backup", "migrate", "release"]
    rows = {r["id"]: r for r in (await client.get(f"/api/projects/{project_id}/deploys")).json()}
    assert rows[staging]["status"] == "replaced" and rows[again]["release"] == 2


async def test_nothing_goes_live_without_both_approvals(
    server: LiveServer, client: httpx.AsyncClient
) -> None:
    host = HostStandIn()
    maria: Person = await person(server, "maria")
    project_id, _ = await app_project(server, maria.web, maria.id)
    url = f"/api/projects/{project_id}/deploys"
    async with host_online(server.url, await host_token(server, host), host):
        staging = await deployed(maria.web, project_id, "staging")
        assert (await until(lambda: settled(maria.web, project_id, staging)))["status"] == "live"
        production = await deployed(maria.web, project_id, "production")
        waiting = await until(lambda: settled(maria.web, project_id, production))
        assert (waiting["status"], waiting["step"]) == ("waiting", "admin")
        assert waiting["creator_approved_at"] > 0  # starting it was the creator's approval
        assert host.kinds("production") == []
        approve = f"/api/admin/hosting/deploys/{production}/approve"
        assert (await maria.web.post(approve)).status_code == 403  # not an admin
        assert (await maria.web.post(url, {"environment": "production"})).status_code == 409
        assert (await client.post(approve)).status_code == 200
        live = await until(lambda: settled(maria.web, project_id, production))
        assert live["status"] == "live" and live["admin"] == "approved", live["error"]
        assert (await client.post(approve)).status_code == 409  # approved once
        async with server.services.db.session() as session, session.begin():
            row = await session.get(User, maria.id)
            assert row is not None
            row.hosting_trusted = True
        trusted = await deployed(maria.web, project_id, "production")
        done = await until(lambda: settled(maria.web, project_id, trusted))
        assert done["status"] == "live" and done["admin"] == "trusted", done["error"]
    await maria.web.client.aclose()


async def test_a_restart_in_the_middle_goes_on_without_migrating_twice(tmp_path: Path) -> None:
    settings = settings_for(tmp_path / "data")
    port = free_port()
    host = HostStandIn()
    host.holds["migrate"] = asyncio.Event()
    with LiveServer(settings, port=port) as first:
        async with httpx.AsyncClient(base_url=first.url, headers=first.headers(),
                                     timeout=60) as client:  # fmt: skip
            project_id, _ = await app_project(first, client, first.services.dev_user_id)
            token = await host_token(first, host)
            online = host_online(first.url, token, host)
            await online.__aenter__()
            deploy_id = await deployed(client, project_id, "staging")
            await until(lambda: asyncio.sleep(0, "migrate" in host.kinds()))
    try:
        with LiveServer(settings, port=port) as second:
            async with httpx.AsyncClient(base_url=second.url, headers=second.headers(),
                                         timeout=60) as client:  # fmt: skip
                host.holds["migrate"].set()  # the host finishes the job it had before
                done = await until(lambda: settled(client, project_id, deploy_id), 90)
    finally:
        await online.__aexit__(None, None, None)
    assert done["status"] == "live" and done["migrated"], done["error"]
    assert host.kinds() == ["check", "migrate", "release"]


async def test_a_failed_health_check_rolls_back_and_says_why(
    server: LiveServer, client: httpx.AsyncClient
) -> None:
    host = HostStandIn()
    project_id, _ = await app_project(server, client, server.services.dev_user_id)
    async with host_online(server.url, await host_token(server, host), host):
        first = await deployed(client, project_id, "staging")
        await until(lambda: settled(client, project_id, first))
        host.results["release"] = JobResult(
            ok=False, release=1, rolled_back=True, log_tail="Traceback: KeyError 'MAPS'",
            error="api did not get healthy; the release was removed",
        )  # fmt: skip
        second = await deployed(client, project_id, "staging")
        failed = await until(lambda: settled(client, project_id, second))
    assert failed["status"] == "failed" and failed["step"] == "release"
    assert "rolled back: api did not get healthy" in failed["error"]
    assert "release before keeps running" in failed["hint"]
    assert "KeyError" in failed["log_tail"]
    rows = {r["id"]: r for r in (await client.get(f"/api/projects/{project_id}/deploys")).json()}
    assert rows[first]["status"] == "live"  # still the one that runs


async def test_secrets_are_sealed_and_fetched_once(
    server: LiveServer, client: httpx.AsyncClient
) -> None:
    host = HostStandIn()
    project_id, _ = await app_project(server, client, server.services.dev_user_id)
    workspace = server.services.driver.workspace(project_id)
    manifest = workspace / "forge.app.toml"
    manifest.write_text(manifest.read_text().replace(
        'clients = ["web"]', 'clients = ["web"]\nsecrets = ["MAPS_KEY"]'))  # fmt: skip
    git(workspace, "commit", "-qam", "the maps key")
    head = await approve_head(server, project_id, server.services.dev_user_id)
    token = await host_token(server, host)
    async with host_online(server.url, token, host):
        deploy_id = await deployed(client, project_id, "staging", head)
        failed = await until(lambda: settled(client, project_id, deploy_id))
        assert failed["status"] == "failed" and "MAPS_KEY" in failed["error"]
        secret = {"environment": "staging", "name": "MAPS_KEY", "value": "maps-s3cr3t"}
        assert (await client.put(f"/api/projects/{project_id}/hosting/secrets",
                                 json=secret)).status_code == 200  # fmt: skip
        retried = await client.post(f"/api/projects/{project_id}/deploys/{deploy_id}/retry")
        assert retried.status_code == 200, retried.text
        live = await until(lambda: settled(client, project_id, deploy_id))
        assert live["status"] == "live", live["error"]
    release = host.last("release")
    assert host.secrets[release.id] == {"MAPS_KEY": "maps-s3cr3t"}
    assert list(host.secrets) == [release.id]  # only the release opens secrets
    async with httpx.AsyncClient(base_url=server.url, timeout=30) as hosted:
        again = await hosted.get(f"/api/host/jobs/{release.id}/secrets",
                                 headers={"Authorization": f"Bearer {token}"})  # fmt: skip
    assert again.status_code == 409  # handed out once
    async with server.services.db.session() as session:
        jobs = list(await session.scalars(select(HostJobRow)))
        stored = list(await session.scalars(select(AppSecret)))
    assert all(not job.sealed and "maps-s3cr3t" not in job.job + job.result for job in jobs)
    assert stored and all("maps-s3cr3t" not in row.value for row in stored)
    names = (await client.get(f"/api/projects/{project_id}/hosting/secrets")).json()
    assert names == [{"environment": "staging", "name": "MAPS_KEY",
                      "updated_at": names[0]["updated_at"]}]  # fmt: skip


async def test_only_an_approved_commit_is_deployed(
    server: LiveServer, client: httpx.AsyncClient
) -> None:
    project_id, head = await app_project(server, client, server.services.dev_user_id)
    url = f"/api/projects/{project_id}/deploys"
    other = await client.post(url, json={"environment": "staging", "commit": "f" * 40})
    assert other.status_code == 409 and "not approved" in other.text
    early = await client.post(url, json={"environment": "production", "commit": head})
    assert early.status_code == 409 and "staging first" in early.text
    no_host = await client.post(url, json={"environment": "staging"})
    assert no_host.status_code == 409 and "no host" in no_host.text
    plain = (await client.post("/api/projects", json={"name": "Plain"})).json()
    code = await client.post(f"/api/projects/{plain['id']}/deploys",
                             json={"environment": "staging"})  # fmt: skip
    assert code.status_code == 409 and "app projects" in code.text
    server.services.settings.hosting.enabled = False
    off = await client.post(url, json={"environment": "staging"})
    assert off.status_code == 403
    assert (await client.get(url)).json() == []
    assert json.loads((await client.get(f"/api/projects/{project_id}/hosting/secrets")).text) == []


GO_LIVE_QUESTION = {"text": "Ready to go live? The release review: fine.", "kind": "choice",
                    "options": [NOT_YET, GO_LIVE, SEND_BACK], "default": NOT_YET,
                    "why": "Only a product you approve is hosted."}  # fmt: skip


async def plain_project(server: LiveServer, client: httpx.AsyncClient) -> str:
    """A plain project with one commit (an app project's chats run the release review first,
    which this script does not play; recording the answer is the same for both)."""
    project_id = str((await client.post("/api/projects", json={"name": "Shop"})).json()["id"])
    workspace = server.services.driver.workspace(project_id)
    (workspace / "README.md").write_text("# Shop\n")
    git(workspace, "add", "-A")
    git(workspace, "commit", "-qm", "start")
    return project_id


async def asked(server: LiveServer, client: httpx.AsyncClient, project_id: str) -> tuple[str, Any]:
    """A new chat of the project, waiting on the go-live question."""
    chat = (await client.post(f"/api/projects/{project_id}/chats", json={})).json()
    async with Browser(server) as tab:
        await tab.send({"type": "subscribe", "chat_id": chat["id"], "after_seq": 0})
        await tab.next("subscribed")
        await client.post(f"/api/chats/{chat['id']}/messages", json={"text": "Ship it"})
        request = (await tab.next_item("request"))["item"]
    return str(chat["id"]), request


def choice(request_id: str, value: str) -> dict[str, Any]:
    return {"request_id": request_id,
            "answer": {"answers": [{"question_index": 0, "values": [value]}]}}  # fmt: skip


async def approvals(server: LiveServer) -> list[GoLiveApproval]:
    async with server.services.db.session() as session:
        return list(await session.scalars(select(GoLiveApproval)))


async def audits(server: LiveServer) -> list[str]:
    async with server.services.db.session() as session:
        return [a.action for a in await session.scalars(select(AuditEntry))]


async def test_ready_to_go_live_in_the_chat_approves_the_commit(tmp_path: Path) -> None:
    assert is_go_live({"questions": [GO_LIVE_QUESTION]})
    assert not is_go_live({"questions": [{**GO_LIVE_QUESTION, "options": [GO_LIVE]}]})
    script = fake_script(call("ask_user", questions=[GO_LIVE_QUESTION]), {"text": "Thanks."})
    with LiveServer(dev_settings(tmp_path / "data", script)) as live:
        async with httpx.AsyncClient(base_url=live.url, headers=live.headers(),
                                     timeout=60) as client:  # fmt: skip
            project_id = await plain_project(live, client)
            waiting, request = await asked(live, client, project_id)
            assert request.get("purpose") == "app_go_live", request
            not_yet = choice(request["id"], NOT_YET)
            assert (await client.post(f"/api/chats/{waiting}/answer", json=not_yet)).json() == {
                "accepted": True}  # fmt: skip
            ready, request = await asked(live, client, project_id)
            go = choice(request["id"], GO_LIVE)
            assert (await client.post(f"/api/chats/{ready}/answer", json=go)).json() == {
                "accepted": True}  # fmt: skip
            [approval] = await until(lambda: approvals(live))
            head = git(live.services.driver.workspace(project_id), "rev-parse", "HEAD")
            actions = await audits(live)
    assert approval.commit == head and approval.chat_id == ready and approval.clean
    assert [a for a in actions if a.startswith("hosting.")] == ["hosting.go_live"]
