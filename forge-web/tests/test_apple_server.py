"""Apple jobs on the server: from a sandbox through the queue to a Mac and back, who may build,
what a Mac may see, and what the server accepts from it."""

import asyncio
import base64
import gzip
import time
from collections.abc import AsyncIterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from forge.ports import AppleBuildResult, AppleScreen
from forge.providers.base import ImagePart
from sqlalchemy import select

from forge_macworker.wire import JobResult
from forge_web.apple.jobs import AppleJobs
from forge_web.apple.sandbox_api import apple_routes
from forge_web.apple.worker_api import worker_router
from forge_web.apple.workers import create_worker, secret_hash, worker_of
from forge_web.db.engine import Database
from forge_web.db.models import AppleJob, Chat, MacWorker, Project, User
from forge_web.gateway.proxy import Gateway
from forge_web.gateway.tokens import run_token
from forge_web.settings import GatewaySettings, WebSettings
from forge_web.vault import Vault
from support import LiveServer, dev_settings

SOURCE = gzip.compress(b"a tar of the project" * 100)
PNG = base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"\0" * 64).decode()


@pytest.fixture
async def world(tmp_path: Path) -> AsyncIterator[SimpleNamespace]:
    db = Database(f"sqlite+aiosqlite:///{tmp_path / 'a.db'}")
    await db.migrate()
    now = time.time()
    async with db.session() as session, session.begin():
        session.add_all([
            User(id="u1", email="a@x", role="member", status="active", created_at=now,
                 apple_allowed=True),
            User(id="u2", email="b@x", role="member", status="active", created_at=now),
            User(id="admin", email="c@x", role="admin", status="active", created_at=now),
        ])  # fmt: skip
        await session.flush()
        session.add(Project(id="p1", name="P", owner_id="u1", created_at=now, updated_at=now))
        await session.flush()
        session.add_all([Chat(id=c, project_id="p1", user_id=u, created_at=now, updated_at=now)
                         for c, u in (("c1", "u1"), ("c2", "u2"), ("c3", "admin"))])  # fmt: skip
    settings = WebSettings(data_dir=tmp_path)
    settings.apple.enabled = True
    jobs = AppleJobs(db, settings.apple, tmp_path)
    working = {"c1", "c2", "c3"}
    gateway = Gateway(db, Vault(b"m" * 32), GatewaySettings(), lambda c: c in working)
    gateway.routers.append(apple_routes(gateway, jobs, settings.apple))
    mac_app = FastAPI()
    mac_app.include_router(worker_router())
    mac_app.state.services = SimpleNamespace(db=db, settings=settings, apple=jobs)
    sandbox = httpx.AsyncClient(transport=httpx.ASGITransport(app=gateway.app()),
                                base_url="http://gw", timeout=30)  # fmt: skip
    mac = httpx.AsyncClient(transport=httpx.ASGITransport(app=mac_app), base_url="http://srv",
                            timeout=60)  # fmt: skip
    _, token = await create_worker(db, "Mac mini")
    yield SimpleNamespace(db=db, settings=settings, jobs=jobs, gateway=gateway, sandbox=sandbox,
                          mac=mac, token=token, working=working, root=tmp_path)  # fmt: skip
    await sandbox.aclose()
    await mac.aclose()
    await gateway.close()
    await db.close()


def run_header(world: SimpleNamespace, chat: str = "c1") -> dict[str, str]:
    return {"Authorization": f"Bearer {run_token(world.gateway.key, chat, 1)}"}


def mac_header(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def ask(world: SimpleNamespace, path: str, chat: str = "c1", body: bytes = SOURCE,
              **query: Any) -> httpx.Response:  # fmt: skip
    return await world.sandbox.post(path, params=query, content=body,
                                    headers=run_header(world, chat))  # fmt: skip


async def poll(world: SimpleNamespace, token: str | None = None) -> dict[str, Any] | None:
    reply = await world.mac.post("/api/mac/poll", json={"free_slots": 1},
                                 headers=mac_header(token or world.token))  # fmt: skip
    assert reply.status_code == 200, reply.text
    job: dict[str, Any] | None = reply.json()["job"]
    return job


async def report(world: SimpleNamespace, job_id: str, result: JobResult,
                 token: str | None = None) -> httpx.Response:  # fmt: skip
    return await world.mac.post(f"/api/mac/jobs/{job_id}/result",
                                content=result.model_dump_json(),
                                headers=mac_header(token or world.token))  # fmt: skip


async def mac_online(world: SimpleNamespace) -> None:
    """A worker that asked for work a moment ago (nothing was queued)."""
    async with world.db.session() as session, session.begin():
        for row in await session.scalars(select(MacWorker)):
            row.last_seen = time.time()


async def test_worker_tokens_are_shown_once_and_kept_as_a_hash(world: SimpleNamespace) -> None:
    row, token = await create_worker(world.db, "Studio")
    secret = token.split("_", 2)[2]
    assert row.token_hash == secret_hash(secret) and secret not in row.token_hash
    assert (await worker_of(world.db, f"Bearer {token}")) is not None
    assert await worker_of(world.db, f"Bearer {token[:-2]}xx") is None
    assert await worker_of(world.db, token) is None  # not a bearer header
    async with world.db.session() as session, session.begin():
        stored = await session.get(MacWorker, row.id)
        assert stored is not None
        stored.enabled = False
    assert await worker_of(world.db, f"Bearer {token}") is None


async def test_a_build_goes_to_a_mac_and_its_result_comes_back(world: SimpleNamespace) -> None:
    waiting = asyncio.create_task(poll(world))
    await asyncio.sleep(0.1)  # the Mac is asking for work now
    request = asyncio.create_task(ask(world, "/apple/build", platform="ios", action="test"))
    job = await waiting
    assert job is not None and job["kind"] == "build" and job["build"]["action"] == "test"
    assert "p1" not in job["project"] and len(job["project"]) == 32  # a key, not the id
    source = await world.mac.get(f"/api/mac/jobs/{job['id']}/source",
                                 headers=mac_header(world.token))  # fmt: skip
    assert source.content == SOURCE
    built = AppleBuildResult(ok=True, platform="ios", action="test", scheme="Tally_iOS",
                             tests_run=2, artifact="/Users/mac/secret/path")  # fmt: skip
    assert (await report(world, job["id"], JobResult(ok=True, build=built))).status_code == 200
    reply = await request
    assert reply.status_code == 200 and reply.json()["tests_run"] == 2
    assert reply.json()["artifact"] == ""  # no archive: the Mac's path is not passed on
    assert not (world.root / "apple" / job["id"] / "source.tar.gz").exists()
    async with world.db.session() as session:
        row = await session.get(AppleJob, job["id"])
    assert row is not None and row.status == "done" and row.user_id == "u1"


async def test_an_archive_stays_on_the_server_under_its_job(world: SimpleNamespace) -> None:
    waiting = asyncio.create_task(poll(world))
    await asyncio.sleep(0.1)
    request = asyncio.create_task(ask(world, "/apple/build", platform="ios", action="archive"))
    job = await waiting
    assert job is not None
    stored = await world.mac.post(f"/api/mac/jobs/{job['id']}/archive", content=b"xcarchive",
                                  headers=mac_header(world.token))  # fmt: skip
    assert stored.status_code == 200
    built = AppleBuildResult(ok=True, platform="ios", action="archive", scheme="Tally_iOS",
                             artifact="/tmp/Tally.xcarchive")  # fmt: skip
    await report(world, job["id"], JobResult(ok=True, build=built))
    assert (await request).json()["artifact"] == f"job:{job['id']}"
    assert (world.root / "apple" / job["id"] / "archive.tar.gz").read_bytes() == b"xcarchive"


async def test_screenshots_must_be_pictures(world: SimpleNamespace) -> None:
    waiting = asyncio.create_task(poll(world))
    await asyncio.sleep(0.1)
    request = asyncio.create_task(ask(world, "/apple/screenshot", platform="ipados", dark="true"))
    job = await waiting
    assert job is not None and job["screenshot"] == {"platform": "ipados", "device": None,
                                                     "dark": True, "fit": None}  # fmt: skip
    fake = JobResult.model_construct(
        ok=True,
        screen=AppleScreen.model_construct(
            platform="ipados",
            device="iPad",
            dark=True,
            image=ImagePart(media_type="image/png", data_b64=base64.b64encode(b"<svg/>").decode()),
        ),
    )
    refused = await world.mac.post(f"/api/mac/jobs/{job['id']}/result",
                                   content=fake.model_dump_json(),
                                   headers=mac_header(world.token))  # fmt: skip
    assert refused.status_code == 422 and "PNG" in refused.text
    screen = AppleScreen(platform="ipados", device="iPad Air", dark=True,
                         image=ImagePart(media_type="image/png", data_b64=PNG))  # fmt: skip
    await report(world, job["id"], JobResult(ok=True, screen=screen))
    reply = await request
    assert reply.status_code == 200 and reply.json()["device"] == "iPad Air"


async def test_a_mac_sees_only_its_own_running_jobs(world: SimpleNamespace) -> None:
    _, other = await create_worker(world.db, "Other Mac")
    assert (await world.mac.post("/api/mac/poll", json={"free_slots": 1},
                                 headers=mac_header("fmw_x_y"))).status_code == 401  # fmt: skip
    waiting = asyncio.create_task(poll(world))
    await asyncio.sleep(0.1)
    request = asyncio.create_task(ask(world, "/apple/build", platform="macos"))
    job = await waiting
    assert job is not None
    stolen = await world.mac.get(f"/api/mac/jobs/{job['id']}/source", headers=mac_header(other))
    assert stolen.status_code == 409
    assert (
        await report(world, job["id"], JobResult(ok=False, error="x"), other)
    ).status_code == 409
    await report(world, job["id"], JobResult(ok=False, error="Xcode is missing", hint="install"))
    reply = await request
    assert reply.status_code == 502 and reply.json() == {"error": "Xcode is missing",
                                                         "hint": "install"}  # fmt: skip
    again = await report(world, job["id"], JobResult(ok=True))
    assert again.status_code == 409  # finished jobs take no second report


async def test_who_may_build(world: SimpleNamespace) -> None:
    assert (await ask(world, "/apple/build", platform="ios")).status_code == 503  # no Mac yet
    await mac_online(world)
    refused = await ask(world, "/apple/build", chat="c2", platform="ios")
    assert refused.status_code == 403 and "ask an admin" in refused.json()["hint"]
    world.settings.apple.allowed = "admins"
    assert (await ask(world, "/apple/build", chat="c1", platform="ios")).status_code == 403
    world.settings.apple.enabled = False
    assert (await ask(world, "/apple/build", chat="c3", platform="ios")).status_code == 403
    world.settings.apple.enabled, world.settings.apple.allowed = True, "granted"
    async with world.db.session() as session, session.begin():
        session.add(AppleJob(id="0" * 32, project_id="p1", chat_id="c1", user_id="u1",
                             kind="build", params="{}", status="done", created_at=time.time(),
                             seconds=world.settings.apple.minutes_per_month * 60))  # fmt: skip
    used_up = await ask(world, "/apple/build", platform="ios")
    assert used_up.status_code == 429 and "minutes" in used_up.json()["error"]


async def test_what_a_sandbox_sends_is_checked(world: SimpleNamespace) -> None:
    await mac_online(world)
    assert (await ask(world, "/apple/build", platform="android")).status_code == 422
    assert (
        await ask(world, "/apple/build", body=b"PK not gzip", platform="ios")
    ).status_code == 400
    world.settings.apple.max_source_mb = 1
    big = await ask(world, "/apple/build", body=b"\x1f\x8b" + b"\0" * (2 * 1024 * 1024),
                    platform="ios")  # fmt: skip
    assert big.status_code == 413
    world.working.discard("c1")
    assert (await ask(world, "/apple/build", platform="ios")).status_code == 401
    assert not list((world.root / "apple" / "incoming").glob("*"))  # refused uploads are gone


async def test_a_job_nobody_takes_ends_in_time(world: SimpleNamespace) -> None:
    await mac_online(world)
    world.settings.apple.job_timeout_s = 0.3
    reply = await ask(world, "/apple/build", platform="ios")
    assert reply.status_code == 502 and "in time" in reply.json()["error"]
    async with world.db.session() as session:
        rows = list(await session.scalars(select(AppleJob)))
    assert [r.status for r in rows] == ["failed"]


def test_admins_add_macs_and_allow_users(tmp_path: Path) -> None:
    settings = dev_settings(tmp_path)
    settings.apple.enabled = True
    with LiveServer(settings) as server:
        added = httpx.post(f"{server.url}/api/admin/apple/macs", json={"name": "Mac mini"},
                           headers=server.headers())  # fmt: skip
        assert added.status_code == 200 and added.json()["token"].startswith("fmw_")
        listed = httpx.get(f"{server.url}/api/admin/apple/macs", headers=server.headers()).json()
        assert [m["name"] for m in listed] == ["Mac mini"] and "token" not in listed[0]
        mac_id = added.json()["id"]
        poll_url = f"{server.url}/api/mac/poll"
        token = {"Authorization": f"Bearer {added.json()['token']}"}
        off = httpx.patch(f"{server.url}/api/admin/apple/macs/{mac_id}", json={"enabled": False},
                          headers=server.headers())  # fmt: skip
        assert off.status_code == 200 and not off.json()["enabled"]
        assert httpx.post(poll_url, json={"free_slots": 1}, headers=token).status_code == 401
        user_id = server.services.dev_user_id
        granted = httpx.put(f"{server.url}/api/admin/apple/users/{user_id}",
                            json={"allowed": True}, headers=server.headers())  # fmt: skip
        assert granted.status_code == 200 and granted.json()["minutes_this_month"] == 0
        people = httpx.get(f"{server.url}/api/admin/apple/users", headers=server.headers()).json()
        assert [(p["id"], p["allowed"]) for p in people] == [(user_id, True)]
        assert httpx.get(f"{server.url}/api/admin/apple/jobs",
                         headers=server.headers()).json() == []  # fmt: skip
        gone = httpx.delete(f"{server.url}/api/admin/apple/macs/{mac_id}", headers=server.headers())
        assert gone.status_code == 200
        assert httpx.get(f"{server.url}/api/admin/apple/macs").status_code == 401  # signed out
