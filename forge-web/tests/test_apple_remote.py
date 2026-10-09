"""Apple builds from a sandbox: what is packed and sent, how answers and errors come back, and a
chat's agent calling apple_build through the gateway."""

import io
import json
import os
import tarfile
from pathlib import Path
from typing import Any

import httpx
import pytest
from forge.ports import AppleBuildError, AppleBuildResult
from pydantic import ValidationError

from forge_sandbox.apple_remote import RemoteAppleBuilder, pack
from forge_sandbox.methods import ChatOptions
from forge_sandbox.worker import ChatWorker, config_overrides
from support import call, fake_script
from test_worker import Collector

PNG = "iVBORw0KGgoAAAAA"
BASE = "http://127.0.0.1:47101"


@pytest.fixture
def project(tmp_path: Path) -> Path:
    root = tmp_path / "Tally"
    (root / "Shared").mkdir(parents=True)
    (root / "project.yml").write_text("name: Tally\n")
    (root / "Shared" / "App.swift").write_text("@main struct App {}\n")
    for skipped in (".git/objects", ".forge/out", "DerivedData/Build", "node_modules/x"):
        (root / skipped).mkdir(parents=True)
        (root / skipped / "big.bin").write_bytes(b"x" * 1000)
    return root


class Service:
    """The gateway's Apple endpoints as the sandbox sees them; records what it got."""

    def __init__(self, status: int = 200, body: Any = None) -> None:
        self.requests: list[httpx.Request] = []
        self.status = status
        self.body = (
            body
            if body is not None
            else AppleBuildResult(
                ok=True, platform="ios", action="build", scheme="Tally_iOS"
            ).model_dump(mode="json")
        )

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if isinstance(self.body, str):
            return httpx.Response(self.status, text=self.body)
        return httpx.Response(self.status, json=self.body)

    def names(self, index: int = 0) -> list[str]:
        """The files in a request's packed project."""
        data = io.BytesIO(self.requests[index].content)
        with tarfile.open(fileobj=data, mode="r:gz") as archive:
            return sorted(member.name for member in archive.getmembers())


def builder(root: Path, service: Service, **kwargs: Any) -> RemoteAppleBuilder:
    return RemoteAppleBuilder(root, BASE, "FW_TEST_TOKEN",
                              transport=httpx.MockTransport(service), **kwargs)  # fmt: skip


async def test_the_project_goes_without_history_and_build_output(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FW_TEST_TOKEN", "fwg.c1.1.sig")
    service = Service()
    result = await builder(project, service).build("ios", "test", scheme="Tally_iOS")
    assert result.ok and result.scheme == "Tally_iOS"
    request = service.requests[0]
    assert request.url.path == "/apple/build"
    assert dict(request.url.params) == {"platform": "ios", "action": "test", "scheme": "Tally_iOS"}
    assert request.headers["authorization"] == "Bearer fwg.c1.1.sig"
    assert service.names() == ["Shared/App.swift", "project.yml"]


async def test_screenshots_come_back_as_pictures(project: Path) -> None:
    screen = {"platform": "watchos", "device": "Apple Watch", "dark": True,
              "image": {"type": "image", "media_type": "image/png", "data_b64": PNG}}  # fmt: skip
    service = Service(body=screen)
    shot = await builder(project, service).screenshot("watchos", dark=True)
    assert shot.device == "Apple Watch" and shot.image.data_b64 == PNG
    assert dict(service.requests[0].url.params) == {"platform": "watchos", "dark": "true"}


async def test_the_services_refusals_reach_the_agent(project: Path) -> None:
    refused = Service(403, {"error": "you may not build Apple apps", "hint": "ask an admin"})
    with pytest.raises(AppleBuildError, match="may not build") as caught:
        await builder(project, refused).build("ios", "build")
    assert caught.value.hint == "ask an admin"
    with pytest.raises(AppleBuildError, match="answered 500"):
        await builder(project, Service(500, "<html>oops</html>")).build("ios", "build")

    def offline(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    gone = RemoteAppleBuilder(project, BASE, "X", transport=httpx.MockTransport(offline))
    with pytest.raises(AppleBuildError, match="could not be reached"):
        await gone.build("macos", "build")


def test_large_projects_are_refused_before_sending(project: Path, tmp_path: Path) -> None:
    (project / "Shared" / "video.mov").write_bytes(os.urandom(2 * 1024 * 1024))
    before = set(Path(os.environ.get("TMPDIR", "/tmp")).glob("forge-apple-*"))
    with pytest.raises(AppleBuildError, match="larger than 1 MB") as caught:
        pack(project, 1024 * 1024)
    assert "large files" in caught.value.hint
    assert set(Path(os.environ.get("TMPDIR", "/tmp")).glob("forge-apple-*")) == before


def test_links_stay_links(project: Path) -> None:
    if os.name == "nt":
        pytest.skip("symbolic links need extra rights on Windows")
    (project / "Shared" / "secrets").symlink_to("/etc")
    packed = pack(project, 10 * 1024 * 1024)
    try:
        with tarfile.open(packed, "r:gz") as archive:
            link = archive.getmember("Shared/secrets")
        assert link.issym() and link.linkname == "/etc"  # the Mac unpacks with the data filter
    finally:
        packed.unlink()


def test_apple_options_turn_on_the_checks() -> None:
    options = ChatOptions(apple_url=BASE, apple_review=True)
    assert config_overrides(options)["apple.review"] is True
    assert config_overrides(options)["permissions.allow"] == ["apple_build", "apple_screenshot"]
    asking = ChatOptions(mode="ask", apple_url=BASE)
    assert config_overrides(asking)["permissions.allow"] == []  # "ask" asks for everything
    assert "apple.review" not in config_overrides(ChatOptions())
    with pytest.raises(ValidationError):
        ChatOptions(apple_url="http://mac.example.com:80")  # only the gateway inside the sandbox


async def test_the_agent_builds_through_the_gateway(
    project: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FORGE_HOME", str(tmp_path / "home"))
    script = fake_script(call("apple_build", platform="ios"), {"text": "It builds."})
    out = Collector()
    options = ChatOptions(mode="edits", fake_script=script, apple_url=BASE)
    worker = ChatWorker(project, "c1", options, out)  # type: ignore[arg-type]
    await worker.start()
    await out.wait_for("ready")
    service = Service()
    assert worker.ctx is not None and isinstance(worker.ctx.state.apple, RemoteAppleBuilder)
    mock = httpx.MockTransport(service)
    worker.ctx.state.apple.client = httpx.AsyncClient(base_url=BASE, transport=mock)
    assert worker.submit("build the app")
    turn = await out.wait_for("turn")
    assert turn["ok"], turn
    finished = [e for e in out.events() if e["kind"] == "tool_finished"]
    assert finished and "build for ios (scheme Tally_iOS): succeeded" in json.dumps(finished)
    assert len(service.requests) == 1
    await worker.close()
