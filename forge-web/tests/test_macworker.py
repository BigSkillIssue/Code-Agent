"""The Mac worker: running a job, direct mode end to end through the server, and VMs per project
(with a stand-in for tart)."""

import asyncio
import io
import json
import sys
import tarfile
from pathlib import Path
from typing import Any

import httpx
import pytest
from forge.ports import AppleAction, AppleBuildResult, ApplePlatform, AppleScreen
from forge.providers.base import ImagePart

from forge_macworker.cli import main
from forge_macworker.client import WorkerClient
from forge_macworker.job import ARCHIVE, JOB, RESULT, SOURCE, run_job
from forge_macworker.runners import MOUNT, DirectRunner, TartRunner, TartSettings
from forge_macworker.wire import JobOffer, JobResult
from test_apple_server import ask, world  # noqa: F401  (the server's Apple world)

PNG = "iVBORw0KGgoAAAAA"
PROJECT = "ab" * 16


def packed(files: dict[str, bytes]) -> bytes:
    """A tar.gz with these files (names as given, even unsafe ones)."""
    data = io.BytesIO()
    with tarfile.open(fileobj=data, mode="w:gz") as archive:
        for name, content in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(content)
            archive.addfile(info, io.BytesIO(content))
    return data.getvalue()


class Builder:
    """Stands in for Xcode: records calls; an archive leaves a folder behind."""

    def __init__(self, root: Path, data: Path) -> None:
        self.root, self.data = root, data
        self.calls: list[tuple[Any, ...]] = []

    async def build(
        self, platform: ApplePlatform, action: AppleAction, scheme: str | None = None
    ) -> AppleBuildResult:
        files = sorted(p.relative_to(self.root).as_posix() for p in self.root.rglob("*.swift"))
        artifact = ""
        if action == "archive":
            archive = self.data / "Tally.xcarchive"
            (archive / "Products").mkdir(parents=True, exist_ok=True)
            artifact = str(archive)
        return AppleBuildResult(ok=True, platform=platform, action=action, scheme="Tally_iOS",
                                log_tail=" ".join(files), artifact=artifact)  # fmt: skip

    async def screenshot(
        self, platform: ApplePlatform, device: str | None = None, dark: bool = False
    ) -> AppleScreen:
        return AppleScreen(platform=platform, device=device or "iPhone 16", dark=dark,
                           image=ImagePart(media_type="image/png", data_b64=PNG))  # fmt: skip

    async def close(self) -> None:
        """Nothing to stop."""


def job_folder(folder: Path, offer: dict[str, Any], source: bytes) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    (folder / JOB).write_text(json.dumps({"project": PROJECT, "timeout_s": 60, **offer}))
    (folder / SOURCE).write_bytes(source)
    return folder


async def test_a_build_job_runs_on_a_fresh_copy_of_the_project(tmp_path: Path) -> None:
    source = packed({"Shared/App.swift": b"@main", "project.yml": b"name: Tally"})
    folder = job_folder(tmp_path / "job", {"id": "1" * 32, "kind": "build",
                        "build": {"platform": "ios", "action": "archive"}}, source)  # fmt: skip
    (tmp_path / "work" / PROJECT / "src" / "Old.swift").parent.mkdir(parents=True)
    (tmp_path / "work" / PROJECT / "src" / "Old.swift").write_text("left over")
    result = await run_job(folder, tmp_path / "work", Builder)
    assert result.ok and result.build is not None and result.build.log_tail == "Shared/App.swift"
    assert JobResult.model_validate_json((folder / RESULT).read_text()) == result
    with tarfile.open(folder / ARCHIVE) as archive:
        assert "Tally.xcarchive/Products" in archive.getnames()


async def test_a_project_that_tries_to_leave_its_folder_is_not_built(tmp_path: Path) -> None:
    def link(name: str, target: str) -> bytes:
        data = io.BytesIO()
        with tarfile.open(fileobj=data, mode="w:gz") as archive:
            info = tarfile.TarInfo(name)
            info.type, info.linkname = tarfile.SYMTYPE, target
            archive.addfile(info)
        return data.getvalue()

    for evil in (packed({"../escape.swift": b"x"}), link("Shared/up", "../../..")):
        folder = job_folder(tmp_path / "job", {"id": "2" * 32, "kind": "build",
                            "build": {"platform": "ios"}}, evil)  # fmt: skip
        result = await run_job(folder, tmp_path / "work", Builder)
        assert not result.ok and "could not be unpacked" in result.error
    assert (
        not (tmp_path / "escape.swift").exists()
        and not (tmp_path / "work" / "escape.swift").exists()
    )
    # An absolute name is kept inside the project folder.
    offer = {"id": "2" * 32, "kind": "build", "build": {"platform": "ios"}}
    folder = job_folder(tmp_path / "job", offer, packed({"/etc/evil.swift": b"x"}))
    result = await run_job(folder, tmp_path / "work", Builder)
    assert result.build is not None and result.build.log_tail == "etc/evil.swift"


async def test_direct_mode_end_to_end(world: Any, tmp_path: Path) -> None:  # noqa: F811
    transport = httpx.ASGITransport(app=world.mac._transport.app)
    stop = asyncio.Event()
    client = WorkerClient("http://srv", world.token, DirectRunner(tmp_path / "mac", Builder),
                          slots=1, transport=transport)  # fmt: skip
    serving = asyncio.create_task(client.serve(stop))
    await asyncio.sleep(0.2)  # the worker is asking for work
    source = packed({"Shared/App.swift": b"@main"})
    reply = await ask(world, "/apple/build", body=source, platform="ios", action="archive")
    assert reply.status_code == 200, reply.text
    assert reply.json()["log_tail"] == "Shared/App.swift"
    assert reply.json()["artifact"].startswith("job:")
    job_id = reply.json()["artifact"][4:]
    assert (world.root / "apple" / job_id / "archive.tar.gz").is_file()
    shot = await ask(world, "/apple/screenshot", body=source, platform="ios", dark="true")
    assert shot.status_code == 200 and shot.json()["dark"] is True
    stop.set()
    await asyncio.wait_for(serving, 30)
    assert not list((tmp_path / "mac" / "jobs").iterdir())  # job folders are removed


FAKE_TART = """#!{python}
import json, os, subprocess, sys
from pathlib import Path

state = Path(os.environ["FAKE_TART_STATE"])
log = state / "calls.jsonl"
with log.open("a") as out:
    out.write(json.dumps(sys.argv[1:]) + "\\n")
command, args = sys.argv[1], sys.argv[2:]
if command == "run":
    shared = next(a for a in args if a.startswith("--dir=jobs:")).split(":", 1)[1]
    (state / (args[0] + ".dir")).write_text(shared)
    import time
    time.sleep(3600)
elif command == "exec" and args[1] != "true":
    shared = (state / (args[0] + ".dir")).read_text()
    folder = args[2].replace("{mount}", shared)
    sys.exit(subprocess.run([*args[1:2], folder]).returncode)
"""

FAKE_JOB = """#!{python}
import json, sys
from pathlib import Path
folder = Path(sys.argv[1])
offer = json.loads((folder / "job.json").read_text())
build = {{"ok": True, "platform": "ios", "action": "build", "scheme": "S",
         "log_tail": folder.parent.name}}
(folder / "result.json").write_text(json.dumps({{"ok": True, "build": build}}))
"""


def fake_tart(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TartSettings:
    state = tmp_path / "tart-state"
    state.mkdir()
    tart, job = tmp_path / "tart", tmp_path / "fake-job"
    tart.write_text(FAKE_TART.format(python=sys.executable, mount=MOUNT))
    job.write_text(FAKE_JOB.format(python=sys.executable))
    for script in (tart, job):
        script.chmod(0o755)
    monkeypatch.setenv("FAKE_TART_STATE", str(state))
    return TartSettings(image="forge-xcode", work=tmp_path / "vms", slots=2, network="softnet",
                        command=str(job), idle_s=0.5, tart=str(tart))  # fmt: skip


def calls(settings: TartSettings) -> list[list[str]]:
    log = Path(settings.work).parent / "tart-state" / "calls.jsonl"
    return [json.loads(line) for line in log.read_text().splitlines()]


@pytest.mark.skipif(sys.platform == "win32", reason="the stand-in for tart is a script")
async def test_each_project_gets_a_vm_of_its_own(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = fake_tart(tmp_path, monkeypatch)
    runner = TartRunner(settings)
    results = []
    for project in ("aa" * 16, "aa" * 16, "bb" * 16):
        folder = await runner.prepare(project, "3" * 32)
        (folder / JOB).write_text("{}")
        results.append(await runner.run(project, folder))
    vms = {r.build.log_tail for r in results if r.build is not None}
    assert len(vms) == 2  # the first project's two jobs shared its VM
    clones = [c for c in calls(settings) if c[0] == "clone"]
    assert [c[1] for c in clones] == ["forge-xcode", "forge-xcode"]
    runs = [c for c in calls(settings) if c[0] == "run"]
    assert all("--net-softnet" in c and "--no-graphics" in c for c in runs)
    await asyncio.sleep(0.6)
    await runner.reap()  # both were idle longer than idle_s
    assert not runner.leases
    deleted = [c[1] for c in calls(settings) if c[0] == "delete"]
    assert sorted(deleted) == sorted(c[2] for c in clones)
    assert not list(settings.work.iterdir())  # shared folders are gone too


def test_the_command_line_needs_a_token_and_says_what_is_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("MAC_WORKER_TOKEN", raising=False)
    assert main(["run", "--server", "https://forge.example.com", "--direct"]) == 2
    assert "token" in capsys.readouterr().err
    monkeypatch.setenv("PATH", str(tmp_path))  # no Xcode, no tart
    assert main(["check", "--direct"]) == 1
    out = capsys.readouterr().out
    assert "xcodebuild is missing" in out and "brew install xcodegen" in out
    assert main(["check"]) == 1
    assert "tart is missing" in capsys.readouterr().out


def test_job_offers_carry_the_right_parameters() -> None:
    with pytest.raises(ValueError, match="build parameters"):
        JobOffer(id="4" * 32, kind="build", project=PROJECT, timeout_s=1)
    with pytest.raises(ValueError, match="screenshot parameters"):
        JobOffer.model_validate({"id": "4" * 32, "kind": "screenshot", "project": PROJECT,
                                 "timeout_s": 1, "build": {"platform": "ios"}})  # fmt: skip
