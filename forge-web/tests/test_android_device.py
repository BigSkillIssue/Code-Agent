"""The host's Android emulator for Forge's agent (W61): the debug APK built in gVisor, the
emulator started with KVM but without privileges, the app installed and started, and the agent's
actions turned into `adb` commands; the host's session loop relays them to the server."""

import asyncio
import io
import struct
import tarfile
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest

from forge_hostworker import android_device
from forge_hostworker.android_device import AndroidDevice, DeviceProblem, elements, find, typed
from forge_hostworker.device_client import DeviceClient
from forge_hostworker.device_wire import DeviceAction, DeviceAnswer, DeviceOffer
from forge_hostworker.docker import DockerResult
from forge_hostworker.runner import HostRunner
from forge_hostworker.screens import png_size
from forge_web.devices import worker_api
from forge_web.devices.sessions import DeviceSession
from host_standin import HostStandIn
from support import LiveServer
from test_hosting_deploy import host_token, settings_for, short_polls  # noqa: F401

SCREEN = b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\x0dIHDR" + struct.pack(">II", 1080, 2400) + b"rest"
UI = """<?xml version='1.0' encoding='UTF-8'?><hierarchy rotation="0">
<node class="android.widget.FrameLayout" text="" content-desc="" resource-id="" clickable="false"
 bounds="[0,0][1080,2400]">
 <node class="android.widget.TextView" text="Opening hours" content-desc="" resource-id=""
  clickable="false" bounds="[40,200][1040,260]"/>
 <node class="android.widget.Button" text="Sign in" content-desc="" resource-id="shop:id/login"
  clickable="true" bounds="[100,2000][980,2140]"/>
 <node class="android.widget.ImageButton" text="" content-desc="Open menu" resource-id=""
  clickable="true" bounds="[0,80][120,200]"/>
</node></hierarchy>"""
OFFER = DeviceOffer(
    id="d" * 32,
    platform="android",
    project="e" * 32,
    app="lehm",
    package="de.lehm.shop",
    build_image="forge/android-build:35",
    device_image="forge/android-emulator:34",
    start_timeout_s=60,
    max_s=600,
)


class EmulatorDocker:
    """Docker that builds an APK and runs an emulator that answers adb as a booted phone."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self.build_fails = False

    async def __call__(
        self, *args: str, timeout: float = 600, out: Path | None = None
    ) -> DockerResult:
        self.calls.append(list(args))
        if args[0] == "run" and "assembleDebug" in args:
            if self.build_fails:
                return DockerResult(1, "", "Compilation failed: MainActivity.kt:3")
            folder = Path(args[args.index("-v") + 1].split(":")[0])
            apk = folder / android_device.APK
            apk.parent.mkdir(parents=True, exist_ok=True)
            apk.write_bytes(b"APK")
        if args[0] == "exec":
            return self.adb(list(args[3:]), out)
        return DockerResult(0, "", "")

    def adb(self, args: list[str], out: Path | None) -> DockerResult:
        if args[:2] == ["shell", "getprop sys.boot_completed"]:
            return DockerResult(0, "1\n", "")
        if args[:2] == ["exec-out", "screencap -p"] and out is not None:
            out.write_bytes(SCREEN)
        if args[:1] == ["exec-out"] and args[1].startswith("cat "):
            return DockerResult(0, UI, "")
        return DockerResult(0, "", "")

    def adb_calls(self) -> list[list[str]]:
        return [call[3:] for call in self.calls if call[0] == "exec"]


def packed(tmp_path: Path, with_app: bool = True) -> Path:
    archive = tmp_path / "source.tar.gz"
    with tarfile.open(archive, "w:gz") as tar:
        files = {"README.md": b"lehm"} | ({"android/gradlew": b"#!/bin/sh"} if with_app else {})
        for name, data in files.items():
            info = tarfile.TarInfo(f"./{name}")
            info.size, info.mode = len(data), 0o755
            tar.addfile(info, io.BytesIO(data))
    return archive


@pytest.fixture
def kvm(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    device = tmp_path / "kvm"
    device.write_text("")
    monkeypatch.setattr(android_device, "KVM", device)
    monkeypatch.setattr(android_device, "SETTLE_S", 0.0)
    yield device


@pytest.fixture
def no_wait(monkeypatch: pytest.MonkeyPatch) -> None:
    async def instant(_seconds: float) -> None:
        return None

    monkeypatch.setattr(android_device.asyncio, "sleep", instant)


def device_for(tmp_path: Path) -> tuple[AndroidDevice, EmulatorDocker]:
    docker = EmulatorDocker()
    runner = HostRunner(docker, tmp_path / "data", user="1000:1000")
    return AndroidDevice(runner, OFFER), docker


def test_the_screen_reads_as_elements_and_taps_find_them() -> None:
    items = elements(UI)
    lines = [item.line() for item in items]
    assert lines[0] == "TextView text='Opening hours' (40,200,1000,60)"
    assert "Button text='Sign in' id='login' (100,2000,880,140) tappable" in lines
    assert find(items, "sign in").center() == (540, 2070)  # type: ignore[union-attr]
    assert find(items, "menu").name == "Open menu"  # type: ignore[union-attr]  # contains
    assert find(items, "checkout") is None and elements("<broken") == []
    assert typed("it's 50% off") == "'it'\"'\"'s%s50\\%%soff'"
    assert png_size(SCREEN) == (1080, 2400) and png_size(b"no") == (0, 0)


async def test_the_app_is_built_installed_and_started_on_a_locked_down_emulator(
    tmp_path: Path, kvm: Path, no_wait: None
) -> None:
    device, docker = device_for(tmp_path)
    first = await device.start(packed(tmp_path))
    assert first.ok and first.seq == 0 and (first.width, first.height) == (1080, 2400)
    build = next(c for c in docker.calls if "assembleDebug" in c)
    assert "--runtime=runsc" in build and "forge-build" in build  # the build runs in gVisor
    emulator = next(c for c in docker.calls if c[:2] == ["run", "-d"])
    assert emulator[emulator.index("--device") + 1] == str(kvm)
    assert "--runtime=runsc" not in emulator  # gVisor has no KVM: hardened otherwise
    for flag in ("--read-only", "no-new-privileges", "ALL", "1000:1000", "forge-build"):
        assert flag in emulator, flag
    assert emulator[-1] == "forge/android-emulator:34"
    adb = docker.adb_calls()
    assert ["install", "-r", "/apk/app-debug.apk"] in adb
    launch = next(c for c in adb if c[0] == "shell" and c[1].startswith("monkey"))
    assert launch[1] == "monkey -p de.lehm.shop -c android.intent.category.LAUNCHER 1"
    await device.close()
    assert docker.calls[-1][:2] == ["rm", "-f"] and not device.work.exists()


async def test_the_agents_actions_become_adb_commands(
    tmp_path: Path, kvm: Path, no_wait: None
) -> None:
    device, docker = device_for(tmp_path)
    await device.start(packed(tmp_path))
    before = len(docker.calls)
    tapped = await device.act(DeviceAction(seq=1, kind="tap", target="Sign in"))
    assert tapped.ok and "Button 'Sign in' at 540,2070" in tapped.text and tapped.image_b64
    await device.act(DeviceAction(seq=2, kind="tap", x=10, y=20))
    await device.act(DeviceAction(seq=3, kind="type", text="maria@lehm.de", submit=True))
    await device.act(DeviceAction(seq=4, kind="swipe", direction="up"))
    await device.act(DeviceAction(seq=5, kind="key", key="back"))
    read = await device.act(DeviceAction(seq=6, kind="read"))
    assert "Open menu" in read.text and not read.image_b64
    shell = [c[1] for c in (call[3:] for call in docker.calls[before:] if call[0] == "exec")
             if c[0] == "shell"]  # fmt: skip
    assert "input tap 540 2070" in shell and "input tap 10 20" in shell
    assert "input text maria@lehm.de" in shell and "input keyevent 66" in shell
    assert "input swipe 540 2000 540 400 300" in shell and "input keyevent 4" in shell
    with pytest.raises(DeviceProblem, match="nothing on the screen"):
        await device.act(DeviceAction(seq=7, kind="tap", target="Checkout"))


async def test_a_host_without_kvm_or_a_failing_build_says_why(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, no_wait: None
) -> None:
    monkeypatch.setattr(android_device, "KVM", tmp_path / "missing")
    device, _ = device_for(tmp_path)
    with pytest.raises(DeviceProblem, match="no /dev/kvm"):
        await device.start(packed(tmp_path))
    monkeypatch.setattr(android_device, "KVM", tmp_path)  # anything that exists
    device, docker = device_for(tmp_path)
    docker.build_fails = True
    with pytest.raises(DeviceProblem, match=r"MainActivity\.kt:3"):
        await device.start(packed(tmp_path))
    with pytest.raises(DeviceProblem, match="no Android app"):
        await device_for(tmp_path)[0].start(packed(tmp_path, with_app=False))


class FakeDevice:
    """A device that starts at once and answers every action with a screen."""

    def __init__(self, _runner: Any, offer: DeviceOffer) -> None:
        self.offer = offer
        self.actions: list[DeviceAction] = []
        self.closed = False
        FakeDevice.made.append(self)

    made: list["FakeDevice"] = []

    async def start(self, source: Path) -> DeviceAnswer:
        assert source.read_bytes()[:2] == b"\x1f\x8b"
        return DeviceAnswer(seq=0, ok=True, text="started", image_b64="AAAA")

    async def act(self, action: DeviceAction) -> DeviceAnswer:
        self.actions.append(action)
        return DeviceAnswer(seq=action.seq, ok=True, text=f"did {action.kind}", image_b64="AAAA")

    async def close(self) -> None:
        self.closed = True


async def test_the_host_relays_a_session_between_the_agent_and_its_device(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from sqlalchemy import select

    from forge_web.db.hosting_models import HostServer
    from forge_web.db.models import Chat
    from test_agent_product import run_header

    monkeypatch.setattr(worker_api, "POLL_WAIT_S", 0.5)
    FakeDevice.made = []
    with LiveServer(settings_for(tmp_path / "data")) as server:
        server.services.gateway.chat_is_working = lambda _chat_id: True
        token = await host_token(server, HostStandIn())
        async with httpx.AsyncClient(base_url=server.url, headers=server.headers()) as web:
            project_id = (await web.post("/api/projects", json={"name": "Lehm"})).json()["id"]
        async with server.services.db.session() as session:
            host_id = await session.scalar(select(HostServer.id))
            chat = await session.scalar(select(Chat.id).where(Chat.project_id == project_id))
        devices = server.services.devices
        devices._record = no_record  # type: ignore[method-assign]  # no real user
        mine = DeviceSession(id="f" * 32, platform="android", project_id=project_id,
                             user_id="u1", chat_id=str(chat), commit="0" * 40, app="lehm",
                             host_id=str(host_id), package="de.lehm.shop")  # fmt: skip
        devices.source(mine).parent.mkdir(parents=True, exist_ok=True)
        devices.source(mine).write_bytes(packed(tmp_path).read_bytes())
        mine.status = "queued"
        devices.sessions[mine.id] = mine  # as if packed: the host's next poll takes it
        stop = asyncio.Event()
        http = httpx.AsyncClient(base_url=server.url, timeout=30,
                                 headers={"Authorization": f"Bearer {token}"})  # fmt: skip
        runner = HostRunner(EmulatorDocker(), tmp_path / "host", user="1000:1000")
        serving = asyncio.create_task(DeviceClient(http, runner, FakeDevice).serve(stop))
        transport = httpx.AsyncHTTPTransport(uds=str(server.services.gateway_server.path))
        headers = run_header(server, str(chat))
        async with httpx.AsyncClient(transport=transport, base_url="http://gw", timeout=60) as gw:
            tapped = (await gw.post("/product/device/act", headers=headers,
                                    json={"kind": "tap", "target": "Sign in"})).json()  # fmt: skip
            assert tapped["ok"] and tapped["text"] == "did tap" and tapped["status"] == "ready"
            assert FakeDevice.made[0].offer.package == "de.lehm.shop"
            await gw.post("/product/device/stop", headers=headers)
        for _ in range(100):
            if FakeDevice.made[0].closed:
                break
            await asyncio.sleep(0.05)
        assert FakeDevice.made[0].closed  # the session ended: the device stopped
        stop.set()
        serving.cancel()
        await asyncio.gather(serving, return_exceptions=True)
        await http.aclose()


async def no_record(_session: DeviceSession) -> None:
    return None
