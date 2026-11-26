"""Forge's agent on devices (W60): it starts its product's Windows (or Android) app on a worker's
device, acts on it and sees the screen afterwards, for its own project only; a worker reaches
only its own sessions; sessions end when stopped, idle or too long, and their minutes count."""

import asyncio
import base64
import time
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from sqlalchemy import select

from forge_hostworker import device_wire as host_wire
from forge_sandbox import product_mcp
from forge_web.db.device_models import DeviceUsage
from forge_web.db.models import AuditEntry, Chat
from forge_web.devices import worker_api
from forge_web.devices.sessions import DeviceSession
from forge_winworker import device_wire as win_wire
from support import LiveServer
from test_agent_product import run_header
from test_hosting_deploy import app_project, git, settings_for, short_polls  # noqa: F401
from test_winworker import short_worker_polls, windows_project, worker_token  # noqa: F401

PNG = base64.b64encode(b"\x89PNG\r\n\x1a\nfake").decode()


@pytest.fixture(autouse=True)
def short_device_polls(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(worker_api, "POLL_WAIT_S", 1.0)


@pytest.fixture
def server(tmp_path: Path) -> Iterator[LiveServer]:
    settings = settings_for(tmp_path / "data")
    settings.stores.windows_enabled = True
    settings.devices.windows = True
    with LiveServer(settings) as live:
        live.services.gateway.chat_is_working = lambda _chat_id: True
        yield live


@pytest.fixture
async def client(server: LiveServer) -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(base_url=server.url, headers=server.headers(), timeout=60) as c:
        yield c


@pytest.fixture
async def gateway(server: LiveServer) -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.AsyncHTTPTransport(uds=str(server.services.gateway_server.path))
    async with httpx.AsyncClient(transport=transport, base_url="http://gw", timeout=60) as gw:
        yield gw


async def chat_of(server: LiveServer, project_id: str) -> tuple[dict[str, str], str]:
    """The run token's header of the project's chat, and the project's HEAD."""
    async with server.services.db.session() as session:
        chat = await session.scalar(select(Chat.id).where(Chat.project_id == project_id))
    head = git(server.services.driver.workspace(project_id), "rev-parse", "HEAD")
    return run_header(server, str(chat)), head


class FakeWorker:
    """A Windows machine's device side, driven step by step by the test."""

    def __init__(self, url: str, token: str) -> None:
        self.http = httpx.AsyncClient(base_url=url, timeout=30,
                                      headers={"Authorization": f"Bearer {token}"})  # fmt: skip

    async def take(self) -> dict[str, Any]:
        for _ in range(30):
            found = (await self.http.post("/api/win/devices/poll")).json()["session"]
            if found:
                return dict(found)
        raise AssertionError("no session came")

    async def next(self, session: str) -> dict[str, Any]:
        for _ in range(30):
            found = (await self.http.post(f"/api/win/devices/{session}/next")).json()
            if found["action"] or found["ended"]:
                return dict(found)
        raise AssertionError("no action came")

    async def answer(self, session: str, **answer: Any) -> httpx.Response:
        return await self.http.post(f"/api/win/devices/{session}/answer", json=answer)


async def test_the_agent_uses_its_windows_app_on_a_device(
    server: LiveServer, client: httpx.AsyncClient, gateway: httpx.AsyncClient
) -> None:
    project_id = await windows_project(server, client)
    headers, head = await chat_of(server, project_id)
    worker = FakeWorker(server.url, await worker_token(client))
    started = await gateway.post("/product/device/start", headers=headers,
                                 json={"platform": "windows", "commit": head})  # fmt: skip
    assert started.status_code == 200, started.text
    offer = await worker.take()
    assert offer["platform"] == "windows" and offer["project"] != project_id
    source = await worker.http.get(f"/api/win/devices/{offer['id']}/source")
    assert source.status_code == 200 and source.content[:2] == b"\x1f\x8b"  # the packed commit
    tap = asyncio.create_task(gateway.post("/product/device/act", headers=headers,
                                           json={"kind": "tap", "target": "Sign in"}))  # fmt: skip
    ready = await worker.answer(offer["id"], seq=0, ok=True, image_b64=PNG, width=1280, height=800)
    assert ready.status_code == 200
    action = (await worker.next(offer["id"]))["action"]
    assert (action["seq"], action["kind"], action["target"]) == (1, "tap", "Sign in")
    await worker.answer(offer["id"], seq=1, ok=True, text="clicked 'Sign in'", image_b64=PNG,
                        width=1280, height=800)  # fmt: skip
    done = (await tap).json()
    assert done["ok"] and done["status"] == "ready" and done["image_b64"] == PNG
    reading = asyncio.create_task(gateway.post("/product/device/act", headers=headers,
                                               json={"kind": "read"}))  # fmt: skip
    action = (await worker.next(offer["id"]))["action"]
    await worker.answer(offer["id"], seq=action["seq"], ok=True,
                        text="Button 'Sign in' (40,60,120,32)")  # fmt: skip
    assert "Button 'Sign in'" in (await reading).json()["text"]
    stopped = await gateway.post("/product/device/stop", headers=headers)
    assert stopped.status_code == 200 and stopped.json()["status"] == "ended"
    assert (await worker.next(offer["id"]))["ended"] is True  # the worker stops the sandbox
    assert (await gateway.post("/product/device/act", headers=headers,
                               json={"kind": "screenshot"})).status_code == 409  # fmt: skip
    async with server.services.db.session() as session:
        usage = list(await session.scalars(select(DeviceUsage)))
        actions = set(await session.scalars(select(AuditEntry.action)))
    assert len(usage) == 1 and usage[0].project_id == project_id
    assert "agent.device_start" in actions
    await worker.http.aclose()


async def test_only_its_own_worker_and_project_reach_a_session(
    server: LiveServer, client: httpx.AsyncClient, gateway: httpx.AsyncClient
) -> None:
    project_id = await windows_project(server, client)
    headers, head = await chat_of(server, project_id)
    mine = FakeWorker(server.url, await worker_token(client, "mine"))
    other = FakeWorker(server.url, await worker_token(client, "other"))
    signer = FakeWorker(server.url, await worker_token(client, "signer", signs=True))
    await gateway.post("/product/device/start", headers=headers,
                       json={"platform": "windows", "commit": head})  # fmt: skip
    offer = await mine.take()
    assert (await other.http.get(f"/api/win/devices/{offer['id']}/source")).status_code == 409
    assert (await other.answer(offer["id"], seq=0, ok=True)).status_code == 409
    assert (await signer.http.post("/api/win/devices/poll")).status_code == 403
    assert (await mine.http.post("/api/host/devices/poll")).status_code == 401  # not a host
    plain, _ = await app_project(server, client, server.services.dev_user_id)
    other_headers, _ = await chat_of(server, plain)
    assert (await gateway.post("/product/device/act", headers=other_headers,
                               json={"kind": "screenshot"})).status_code == 409  # fmt: skip
    for worker in (mine, other, signer):
        await worker.http.aclose()


async def test_what_refuses_a_device(
    server: LiveServer, client: httpx.AsyncClient, gateway: httpx.AsyncClient
) -> None:
    project_id = await windows_project(server, client)
    headers, head = await chat_of(server, project_id)

    async def start(platform: str) -> httpx.Response:
        return await gateway.post("/product/device/start", headers=headers,
                                  json={"platform": platform, "commit": head})  # fmt: skip

    assert (await start("windows")).status_code == 503  # no Windows machine connected
    assert (await start("android")).status_code == 403  # Android devices are off
    server.services.settings.devices.android = True
    server.services.settings.devices.android_image = "forge/android-emulator:34"
    server.services.settings.hosting.android_image = "forge/android-build:35"
    assert (await start("android")).status_code == 409  # not on a host yet
    await worker_token(client)
    server.services.settings.devices.minutes_per_month = 5
    async with server.services.db.session() as session, session.begin():
        session.add(DeviceUsage(id="u1", user_id=server.services.dev_user_id,
                                project_id=project_id, platform="windows",
                                started_at=time.time() - 600, ended_at=time.time()))  # fmt: skip
    used_up = await start("windows")
    assert used_up.status_code == 429 and "device minutes" in used_up.text
    server.services.settings.devices.minutes_per_month = 0
    plain, _ = await app_project(server, client, server.services.dev_user_id)
    plain_headers, plain_head = await chat_of(server, plain)
    await gateway.post("/product/device/start", headers=plain_headers,
                       json={"platform": "windows", "commit": plain_head})  # fmt: skip
    status: dict[str, Any] = {}
    for _ in range(50):
        status = (await gateway.get("/product/device", headers=plain_headers)).json()
        if status["status"] == "ended":
            break
        await asyncio.sleep(0.1)
    assert status["status"] == "ended" and "windows/App" in status["error"]


async def test_idle_and_overlong_sessions_end(server: LiveServer) -> None:
    devices = server.services.devices
    now = time.time()
    idle = DeviceSession(id="a" * 32, platform="windows", project_id="p1", user_id="u",
                         chat_id="c", commit="0" * 40, status="ready", started=now - 60,
                         last_used=now - 6 * 60)  # fmt: skip
    long = DeviceSession(id="b" * 32, platform="windows", project_id="p2", user_id="u",
                         chat_id="c", commit="0" * 40, status="ready", started=now - 31 * 60,
                         last_used=now)  # fmt: skip
    fresh = DeviceSession(id="c" * 32, platform="windows", project_id="p3", user_id="u",
                          chat_id="c", commit="0" * 40, status="ready", started=now - 60,
                          last_used=now)  # fmt: skip
    for session in (idle, long, fresh):
        devices.sessions[session.id] = session
    devices._record = _no_record  # type: ignore[method-assign]  # the users are not real
    await devices.expire(now)
    assert idle.status == long.status == "ended" and fresh.status == "ready"
    assert "no action" in idle.error and "at most 30" in long.error


async def _no_record(session: DeviceSession) -> None:
    return None


def test_both_workers_speak_the_same_device_wire() -> None:
    assert Path(host_wire.__file__).read_text() == Path(win_wire.__file__).read_text()


async def test_the_device_tools_show_the_screen(monkeypatch: pytest.MonkeyPatch) -> None:
    sent: list[dict[str, Any]] = []

    async def answer(self: Any, method: str, path: str, body: Any = None) -> dict[str, Any]:
        sent.append(body)
        return {"status": "ready", "ok": True, "text": "clicked", "image_b64": PNG,
                "media_type": "image/png", "width": 1080, "height": 2400}  # fmt: skip

    monkeypatch.setattr(product_mcp.Tools, "call_json", answer)
    tools = product_mcp.Tools(Path("."), "http://gw", "TOKEN", "apps.example")
    found = await product_mcp.act(tools, {"kind": "tap", "x": 10, "y": 20})
    assert "1080x2400" in found[0] and found[1].data.startswith(b"\x89PNG")
    mcp = product_mcp.server(tools)
    await mcp.call_tool("device_tap", {"target": "540, 1200"})
    await mcp.call_tool("device_tap", {"target": "Sign in"})
    assert sent[1] == {"kind": "tap", "x": 540, "y": 1200} and sent[2]["target"] == "Sign in"
