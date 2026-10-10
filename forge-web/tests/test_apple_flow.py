"""The whole Apple flow (W23): a chat in an Apple project has its request, plan and app checked by
the guideline reviewer, builds, tests and photographs the app on a Mac through the server, and
the user approves it.

With a stand-in for Xcode this runs everywhere; `pytest -m mac` runs it with the real Xcode and a
`forge-mac-worker` in direct mode on this Mac (CI does, on a macOS runner, where VMs are not
possible).
"""

import asyncio
import base64
import contextlib
import json
import os
import shutil
import subprocess
import sys
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from forge.apple_flow import APPROVE
from forge.ports import AppleAction, AppleBuildResult, ApplePlatform, AppleScreen
from forge.providers.base import ImagePart

from forge_macworker.client import WorkerClient
from forge_macworker.runners import DirectRunner
from support import LiveServer, dev_settings

SPEC = {"goal": "A tally counter app", "context": "", "requirements": ["counts taps"],
        "constraints": [], "acceptance_criteria": ["the number goes up"], "assumptions": [],
        "open_questions": [], "size": "small"}  # fmt: skip
STEPS = [{"title": "A greeting", "detail": "a constant the app can show", "check": "review: done"}]
GREETING = {"path": "Shared/Greeting.swift",
            "content": 'enum Greeting {\n    static let text = "Hello"\n}\n'}  # fmt: skip
DEVICES = {("ios", False), ("ios", True), ("ipados", False), ("ipados", True), ("macos", False),
           ("macos", True), ("watchos", True)}  # fmt: skip
PNG = base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"\0" * 32).decode()
NO_TESTS = "Scheme Tally_watchOS is not currently configured for the test action."


def review(summary: str) -> dict[str, Any]:
    """The guideline reviewer finds nothing wrong."""
    areas = ("safety", "performance", "business", "design", "legal", "hig")
    findings = [{"area": area, "status": "ok", "reason": "fine"} for area in areas]
    return {"text": json.dumps({"summary": summary, "findings": findings})}


def script() -> dict[str, Any]:
    """Every model call of an Apple chat, by role (the fake model answers each in turn)."""
    return {"roles": {
        "apple_reviewer": [review("A tally counter is fine."), review("The plan is fine."),
                           review("Ready to ship.")],
        "refiner": [{"text": json.dumps(SPEC)}],
        "planner": [{"tool_calls": [{"name": "submit_plan", "arguments": {"steps": STEPS}}]},
                    {"text": "planned"}],
        "coder": [{"tool_calls": [{"name": "write_file", "arguments": GREETING}]},
                  {"text": "written"}],
        "reviewer": [{"text": '{"pass": true, "reason": "ok"}'},
                     {"text": '{"ok": true, "summary": "A greeting was added."}'}],
    }}  # fmt: skip


class StandIn:
    """Xcode's stand-in: every build passes (the Watch app has no tests), every picture is a PNG."""

    calls: list[tuple[str, ...]] = []

    def __init__(self, root: Path, data: Path) -> None:
        self.root = root

    async def build(
        self, platform: ApplePlatform, action: AppleAction, scheme: str | None = None
    ) -> AppleBuildResult:
        StandIn.calls.append(("build", platform, action))
        assert (self.root / "Shared" / "Greeting.swift").is_file()  # the coder's file came along
        if platform == "watchos" and action == "test":
            return AppleBuildResult(ok=False, platform=platform, action=action,
                                    scheme="Tally_watchOS", log_tail=NO_TESTS)  # fmt: skip
        return AppleBuildResult(ok=True, platform=platform, action=action, scheme="Tally",
                                tests_run=2 if action == "test" else 0)  # fmt: skip

    async def screenshot(
        self, platform: ApplePlatform, device: str | None = None, dark: bool = False
    ) -> AppleScreen:
        StandIn.calls.append(("screenshot", platform, "dark" if dark else "light"))
        return AppleScreen(platform=platform, device=device or platform, dark=dark,
                           image=ImagePart(media_type="image/png", data_b64=PNG))  # fmt: skip

    async def close(self) -> None:
        """Nothing to stop."""


@pytest.fixture
def server(tmp_path: Path) -> Iterator[LiveServer]:
    settings = dev_settings(tmp_path / "data", script())
    settings.apple.enabled = True
    settings.apple.allowed = "everyone"
    with LiveServer(settings) as live:
        yield live


@pytest.fixture
async def client(server: LiveServer) -> AsyncIterator[httpx.AsyncClient]:
    # A fresh connection per request: a long wait must not trip over a closing keep-alive one.
    fresh = httpx.Limits(max_keepalive_connections=0)
    async with httpx.AsyncClient(base_url=server.url, headers=server.headers(), timeout=120,
                                 limits=fresh) as c:  # fmt: skip
        yield c


async def until(check: Callable[[], Awaitable[Any]], timeout: float, every: float = 1) -> Any:
    """Wait for `check()` to return something truthy, and return it (a dropped connection on
    the way is asked again: a busy CI Mac sometimes drops one)."""
    async with asyncio.timeout(timeout):
        while True:
            try:
                if found := await check():
                    return found
            except httpx.TransportError as err:
                print(f"asking again after {err!r}")
            await asyncio.sleep(every)


async def new_mac(client: httpx.AsyncClient) -> str:
    """A Mac's token from the admin API."""
    return str((await client.post("/api/admin/apple/macs", json={"name": "ci"})).json()["token"])


async def online(client: httpx.AsyncClient) -> bool:
    return any(m["online"] for m in (await client.get("/api/admin/apple/macs")).json())


async def approve_the_app(client: httpx.AsyncClient, timeout: float) -> dict[str, Any]:
    """Ask for an app, wait for Forge's approval question, approve; the approval page's data."""
    project = (await client.post("/api/projects", json={"name": "Tally", "source": "apple"})).json()
    chat = (await client.post(f"/api/projects/{project['id']}/chats", json={"mode": "auto"})).json()
    await client.post(f"/api/chats/{chat['id']}/messages", json={"text": "Greet the user."})
    review_url = f"/api/projects/{project['id']}/apple/review"

    async def asked() -> dict[str, Any] | None:
        pending: dict[str, Any] | None = (await client.get(review_url)).json()["pending"]
        return pending

    try:
        pending = await until(asked, timeout, every=2)
    except TimeoutError:
        await tell_what_happened(client, chat["id"])
        raise
    body = {"request_id": pending["request_id"],
            "answer": {"answers": [{"question_index": 0, "values": [APPROVE]}]}}  # fmt: skip
    assert (await client.post(f"/api/chats/{chat['id']}/answer", json=body)).json()["accepted"]
    page: dict[str, Any] = (await client.get(review_url)).json()
    screens = await client.get(f"/api/projects/{project['id']}/apple/screens")
    return {**page, "screens": screens.json()}


async def tell_what_happened(client: httpx.AsyncClient, chat_id: str) -> None:
    """The chat's last items and the Mac jobs, for a run that never got to the approval."""
    items = (await client.get(f"/api/chats/{chat_id}/events")).json()["items"]
    for entry in items[-40:]:
        print(json.dumps(entry["item"])[:2000])
    for job in (await client.get("/api/admin/apple/jobs")).json():
        print(job["kind"], job["params"], job["status"], job["outcome"][:500])


def check_the_page(page: dict[str, Any]) -> None:
    """Every stage reviewed, the builds done, every device photographed, the approval kept."""
    assert [r["stage"] for r in page["reviews"]] == ["prompt", "plan", "product"]
    assert all(r["verdict"] == "ok" for r in page["reviews"])
    [approval] = page["approvals"]
    assert approval["commit"] and approval["summary"] == "Ready to ship."
    builds = {(b["params"].get("platform"), b["params"].get("action")): b for b in page["builds"]
              if b["kind"] == "build"}  # fmt: skip
    for platform in ("ios", "macos"):
        assert builds[(platform, "test")]["status"] == "done", builds[(platform, "test")]
    assert builds[("watchos", "build")]["status"] == "done", builds[("watchos", "build")]
    assert {(s["platform"], s["dark"]) for s in page["screens"]} == DEVICES


async def test_the_whole_flow_with_a_stand_in_mac(
    server: LiveServer, client: httpx.AsyncClient, tmp_path: Path
) -> None:
    StandIn.calls = []
    worker = WorkerClient(
        server.url, await new_mac(client), DirectRunner(tmp_path / "mac", StandIn)
    )
    stop = asyncio.Event()
    serving = asyncio.create_task(worker.serve(stop))
    try:
        await until(lambda: online(client), 30, every=0.2)
        page = await approve_the_app(client, timeout=120)
    finally:
        stop.set()
        serving.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await serving
    check_the_page(page)
    builds = [c for c in StandIn.calls if c[0] == "build"]
    assert builds == [("build", "ios", "test"), ("build", "macos", "test"),
                      ("build", "watchos", "test"), ("build", "watchos", "build")]  # fmt: skip


@pytest.mark.mac
@pytest.mark.skipif(sys.platform != "darwin" or shutil.which("xcodegen") is None,
                    reason="needs a Mac with Xcode and XcodeGen")  # fmt: skip
async def test_the_whole_flow_on_this_mac_with_xcode(
    server: LiveServer, client: httpx.AsyncClient, tmp_path: Path
) -> None:
    token_file = tmp_path / "token"
    token_file.write_text(await new_mac(client))
    token_file.chmod(0o600)
    argv = [sys.executable, "-m", "forge_macworker", "run", "--server", server.url, "--direct",
            "--token-file", str(token_file), "--work", str(tmp_path / "mac")]  # fmt: skip
    worker = subprocess.Popen(argv)
    try:
        await until(lambda: online(client), 60)
        page = await approve_the_app(client, timeout=50 * 60)  # real builds and simulators
    finally:
        worker.terminate()
        try:
            worker.wait(30)
        except subprocess.TimeoutExpired:
            worker.kill()
            worker.wait(30)
    check_the_page(page)
    if os.environ.get("APPLE_SHOTS_DIR"):
        keep(page["screens"], Path(os.environ["APPLE_SHOTS_DIR"]))


def keep(screens: list[dict[str, Any]], folder: Path) -> None:
    """Keep the pictures for the CI artifact."""
    folder.mkdir(parents=True, exist_ok=True)
    for shot in screens:
        name = f"web-{shot['platform']}-{'dark' if shot['dark'] else 'light'}.png"
        (folder / name).write_bytes(base64.b64decode(shot["url"].split(",", 1)[1]))
