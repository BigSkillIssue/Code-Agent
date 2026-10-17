"""Windows builds (W38a, W39): jobs go from the server to the Windows machines (the real
`forge-win-worker` client, over HTTP) and back. A build machine tests and publishes the app and
packs it with Velopack; with a signing machine, the build machine sends the published app and
the signing machine packs and signs it, and never builds. The release is checked and kept, the
version rises with every build, a failing test fails the build, a worker only ever sees its own
running jobs, and the worker's token never reaches a job. dotnet and vpk are stand-ins that write
what the real ones would."""

import asyncio
import contextlib
import hashlib
import json
import os
import tarfile
import zipfile
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest

from forge_web.stores import windows_api
from forge_web.stores.windows_files import (
    ReleaseProblem,
    check_release,
    signature_size,
    unpack_release,
)
from forge_winworker.build import SourceError, assembly_name, counted, safe_parts, unpack
from forge_winworker.client import WorkerClient
from forge_winworker.runners import DirectRunner, job_environment
from forge_winworker.wire import BuildParams, JobOffer, PackParams
from support import LiveServer
from test_hosting_deploy import app_project, approve_head, git, settings_for, short_polls, until

__all__ = ["short_polls"]
PROJECT = "<Project><PropertyGroup><AssemblyName>Shop</AssemblyName></PropertyGroup></Project>\n"
WINDOWS = {"windows/App/App.csproj": PROJECT, "windows/Tests/Tests.csproj": "<Project />\n"}
TRX = (
    '<TestRun xmlns="http://microsoft.com/schemas/VisualStudio/TeamTest/2010"><ResultSummary>'
    '<Counters total="{n}" passed="{passed}" failed="{failed}" /></ResultSummary></TestRun>'
)


def program(signed: bool) -> bytes:
    """A Windows program's headers (PE32+), with or without an Authenticode signature."""
    head = bytearray(1024)
    head[0:2], pe = b"MZ", 0x80
    head[0x3C:0x40] = pe.to_bytes(4, "little")
    head[pe : pe + 4] = b"PE\0\0"
    optional = pe + 24
    head[optional : optional + 2] = (0x20B).to_bytes(2, "little")
    head[optional + 108 : optional + 112] = (16).to_bytes(4, "little")  # data directories
    security = optional + 112 + 8 * 4
    head[security + 4 : security + 8] = (2048 if signed else 0).to_bytes(4, "little")
    return bytes(head)


def velopack_release(out: Path, pack_id: str, version: str, signed: bool) -> None:
    """What `vpk pack` writes."""
    out.mkdir(parents=True, exist_ok=True)
    (out / f"{pack_id}-win-Setup.exe").write_bytes(program(signed) + b"setup")
    (out / f"{pack_id}-win-Portable.zip").write_bytes(b"PK\x05\x06" + b"\0" * 18)
    package = out / f"{pack_id}-{version}-full.nupkg"
    package.write_bytes(b"PK the full package " + version.encode())
    asset = {"PackageId": pack_id, "Version": version, "Type": "Full",
             "FileName": package.name, "Size": package.stat().st_size,
             "SHA256": hashlib.sha256(package.read_bytes()).hexdigest().upper()}  # fmt: skip
    (out / "releases.win.json").write_text(json.dumps({"Assets": [asset]}), encoding="utf-8")


class FakeTools(DirectRunner):
    """Runs no dotnet or vpk: `test` writes a TRX file, `publish` the app's .exe, `vpk pack`
    the release (its installer signed when a signing command was given)."""

    def __init__(self, work: Path) -> None:
        super().__init__(work)
        self.failing = False
        self.signs = True  # whether the signing command really signs
        self.commands: list[list[str]] = []
        self.environments: list[dict[str, str]] = []

    async def run(self, command: list[str], cwd: Path) -> tuple[int, str]:
        self.commands.append(command)
        self.environments.append(job_environment())
        if command[:2] == ["dotnet", "test"]:
            results = Path(command[command.index("--results-directory") + 1])
            results.mkdir(parents=True, exist_ok=True)
            failed = 1 if self.failing else 0
            (results / "run.trx").write_text(TRX.format(n=3, passed=3 - failed, failed=failed))
            return (1, "Failed AccountTests.SignIn") if self.failing else (0, "Passed!")
        if command[:2] == ["dotnet", "publish"]:
            out = Path(command[command.index("-o") + 1])
            out.mkdir(parents=True)
            (out / "Shop.exe").write_bytes(program(False))
            (out / "Shop.dll").write_bytes(b"the app")
            return 0, "Build succeeded."
        value = {command[i]: command[i + 1] for i in range(2, len(command) - 1, 2)}
        signed = "--signTemplate" in value and self.signs
        velopack_release(Path(value["--outputDir"]), value["--packId"], value["--packVersion"],
                         signed)  # fmt: skip
        return 0, f"vpk packed {value['--packId']}"


@pytest.fixture
def server(tmp_path: Path) -> Iterator[LiveServer]:
    settings = settings_for(tmp_path / "data")
    settings.stores.windows_enabled = True
    with LiveServer(settings) as live:
        yield live


@pytest.fixture
async def client(server: LiveServer) -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(base_url=server.url, headers=server.headers(), timeout=60) as c:
        yield c


@pytest.fixture(autouse=True)
def short_worker_polls(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(windows_api, "POLL_WAIT_S", 1.0)


async def worker_token(client: httpx.AsyncClient, name: str = "win-1", signs: bool = False) -> str:
    made = await client.post(
        "/api/admin/stores/windows/workers", json={"name": name, "signs": signs}
    )
    assert made.status_code == 200, made.text
    return str(made.json()["token"])


@contextlib.asynccontextmanager
async def worker_online(
    url: str, token: str, runner: FakeTools, sign_template: str = ""
) -> AsyncIterator[None]:
    """The real worker client, taking jobs while the block runs."""
    worker = WorkerClient(url, token, runner, sign_template=sign_template)
    stop = asyncio.Event()
    serving = asyncio.create_task(worker.serve(stop))
    try:
        yield
    finally:
        stop.set()
        with contextlib.suppress(asyncio.CancelledError, TimeoutError):
            await asyncio.wait_for(serving, 10)


async def windows_project(server: LiveServer, client: httpx.AsyncClient) -> str:
    """An app project whose approved commit has a Windows app."""
    project_id, _ = await app_project(server, client, server.services.dev_user_id)
    workspace = server.services.driver.workspace(project_id)
    for name, text in WINDOWS.items():
        (workspace / name).parent.mkdir(parents=True, exist_ok=True)
        (workspace / name).write_text(text, encoding="utf-8")
    git(workspace, "add", "-A")
    git(workspace, "commit", "-qm", "the Windows app")
    await approve_head(server, project_id, server.services.dev_user_id)
    return project_id


async def started(client: httpx.AsyncClient, project_id: str) -> dict[str, Any]:
    answer = await client.post(f"/api/projects/{project_id}/windows/builds")
    assert answer.status_code == 200, answer.text
    return dict(answer.json())


async def finished(client: httpx.AsyncClient, project_id: str, build_id: str) -> Any:
    rows = (await client.get(f"/api/projects/{project_id}/windows/builds")).json()
    row = next(r for r in rows if r["id"] == build_id)
    return row if row["status"] in ("built", "failed") else None


RELEASE = {"Shop-win-Setup.exe", "Shop-win-Portable.zip", "releases.win.json"}


async def test_a_build_is_tested_packed_and_kept(
    server: LiveServer, client: httpx.AsyncClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("WIN_WORKER_TOKEN", "fww_never_in_a_job")
    runner = FakeTools(tmp_path / "worker")
    async with worker_online(server.url, await worker_token(client), runner):
        project_id = await windows_project(server, client)
        first = await started(client, project_id)
        done = await until(lambda: finished(client, project_id, first["id"]))
        assert done["status"] == "built", done["error"]
        assert (done["version"], done["tests_passed"], done["signed"]) == ("1.0.1", 3, False)
        assert set(done["files"]) == RELEASE | {"Shop-1.0.1-full.nupkg"}
        url = f"/api/projects/{project_id}/windows/builds/{first['id']}/package"
        setup = await client.get(url)
        assert setup.status_code == 200 and setup.content.startswith(b"MZ")
        second = await started(client, project_id)
        again = await until(lambda: finished(client, project_id, second["id"]))
        assert again["version"] == "1.0.2" and "Shop-1.0.2-full.nupkg" in again["files"]
    publish = next(c for c in runner.commands if c[:2] == ["dotnet", "publish"])
    assert "-p:Version=1.0.1" in publish
    pack = next(c for c in runner.commands if c[:2] == ["vpk", "pack"])
    assert "--signTemplate" not in pack and pack[pack.index("--packTitle") + 1] == "Shop"
    assert all("WIN_WORKER_TOKEN" not in env for env in runner.environments)
    assert not list((tmp_path / "worker" / "jobs").iterdir())  # nothing stays on the machine


async def test_a_signing_machine_packs_and_signs_and_never_builds(
    server: LiveServer, client: httpx.AsyncClient, tmp_path: Path
) -> None:
    builder, signer = FakeTools(tmp_path / "builder"), FakeTools(tmp_path / "signer")
    build_token = await worker_token(client, "build")
    sign_token = await worker_token(client, "sign", signs=True)
    template = "signtool sign /fd sha256 {{file}}"
    async with (worker_online(server.url, build_token, builder),
                worker_online(server.url, sign_token, signer, template)):  # fmt: skip
        project_id = await windows_project(server, client)
        build = await started(client, project_id)
        assert build["signed"] is True  # a signing machine is there
        done = await until(lambda: finished(client, project_id, build["id"]))
        assert done["status"] == "built" and done["signed"] is True, done["error"]
        assert done["log_tail"] == "(the signing machine keeps its log)"
        signer.signs = False  # a signing command that signs nothing is found out
        broken = await started(client, project_id)
        failed = await until(lambda: finished(client, project_id, broken["id"]))
        assert failed["status"] == "failed" and "not signed" in failed["error"]
    assert not any(c[0] == "vpk" for c in builder.commands)  # the build machine packs nothing
    assert all(c[0] == "vpk" for c in signer.commands)  # and the signing machine builds nothing
    assert signer.commands[0][signer.commands[0].index("--signTemplate") + 1] == template
    assert "--skipVeloAppCheck" in signer.commands[0]  # vpk never starts the product's program


async def test_a_failing_test_or_a_missing_app_fails_the_build(
    server: LiveServer, client: httpx.AsyncClient, tmp_path: Path
) -> None:
    runner = FakeTools(tmp_path / "worker")
    runner.failing = True
    async with worker_online(server.url, await worker_token(client), runner):
        project_id = await windows_project(server, client)
        build = await started(client, project_id)
        failed = await until(lambda: finished(client, project_id, build["id"]))
        assert failed["status"] == "failed" and failed["error"] == "the Windows app's tests failed"
        assert failed["tests_failed"] == 1 and "AccountTests" in failed["log_tail"]
        url = f"/api/projects/{project_id}/windows/builds/{build['id']}/package"
        assert (await client.get(url)).status_code == 409
        plain, _ = await app_project(server, client, server.services.dev_user_id)
        missing = await started(client, plain)
        none = await until(lambda: finished(client, plain, missing["id"]))
        assert "windows/App" in none["error"] and "--windows" in none["hint"]


async def test_a_worker_sees_only_its_own_running_jobs(
    server: LiveServer, client: httpx.AsyncClient
) -> None:
    project_id = await windows_project(server, client)
    mine, other = await worker_token(client, "mine"), await worker_token(client, "other")
    signer = await worker_token(client, "signer", signs=True)
    build = await started(client, project_id)
    async with httpx.AsyncClient(base_url=server.url, timeout=30) as raw:

        async def as_worker(token: str, method: str, path: str, **kwargs: Any) -> httpx.Response:
            headers = {"Authorization": f"Bearer {token}"}
            return await raw.request(method, path, headers=headers, **kwargs)

        assert (await as_worker("fww_x_y", "POST", "/api/win/poll",
                                json={"free_slots": 1})).status_code == 401  # fmt: skip
        assert await poll_job(as_worker, signer) is None  # a signing machine gets no build
        polled = await until(lambda: poll_job(as_worker, mine))
        offer = JobOffer.model_validate(polled)
        assert offer.id == build["id"] and offer.kind == "build"
        assert offer.build == BuildParams(version="1.0.1", title="Shop", pack=False)
        assert set(polled) == {"id", "kind", "project", "build", "pack", "timeout_s"}  # no secrets
        source = f"/api/win/jobs/{offer.id}/source"
        assert (await as_worker(other, "GET", source)).status_code == 409
        assert (await as_worker(mine, "GET", source)).status_code == 200
        result = f"/api/win/jobs/{offer.id}/result"
        late = await as_worker(other, "POST", result, content=b'{"ok": true}')
        assert late.status_code == 409
    rows = (await client.get("/api/admin/stores/windows/workers")).json()
    assert {(r["name"], r["signs"]) for r in rows} == {("mine", False), ("other", False),
                                                       ("signer", True)}  # fmt: skip
    assert "token" not in rows[0]


async def poll_job(as_worker: Any, token: str) -> dict[str, Any] | None:
    answer = await as_worker(token, "POST", "/api/win/poll", json={"free_slots": 1})
    assert answer.status_code == 200, answer.text
    job = answer.json()["job"]
    return dict(job) if job else None


async def test_each_machine_refuses_the_other_kind_of_job(tmp_path: Path) -> None:
    pack = PackParams(version="1.0.1", title="Shop", pack_id="Shop")
    build = BuildParams(version="1.0.1", title="Shop")
    offers = {"build": JobOffer(id="a" * 32, kind="build", project="b" * 32, build=build,
                                timeout_s=60),
              "pack": JobOffer(id="c" * 32, kind="pack", project="b" * 32, pack=pack,
                               timeout_s=60)}  # fmt: skip
    builder = WorkerClient("http://x", "fww_a_b", FakeTools(tmp_path))
    signer = WorkerClient("http://x", "fww_a_b", FakeTools(tmp_path), sign_template="s {{file}}")
    assert builder.refusal(offers["build"]) == "" and "signs nothing" in builder.refusal(
        offers["pack"]
    )
    assert signer.refusal(offers["pack"]) == "" and "never builds" in signer.refusal(
        offers["build"]
    )
    for worker in (builder, signer):
        await worker.http.aclose()


def test_the_steps_and_checks_on_their_own(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    archive = tmp_path / "source.tar.gz"
    with tarfile.open(archive, "w:gz") as tar:
        project = tmp_path / "p"
        project.write_text(PROJECT, encoding="utf-8")
        tar.add(project, "windows/App/App.csproj")
    unpack(archive, tmp_path / "src")
    assert assembly_name(tmp_path / "src" / "windows/App/App.csproj") == "Shop"
    (tmp_path / "bad.csproj").write_text("<AssemblyName>..\\evil</AssemblyName>")
    with pytest.raises(SourceError):
        assembly_name(tmp_path / "bad.csproj")
    results = tmp_path / "results"
    results.mkdir()
    (results / "a.trx").write_text(TRX.format(n=5, passed=4, failed=1))
    assert counted(results) == (4, 1)
    assert safe_parts("app/sub/Shop.dll") == ("sub", "Shop.dll")
    for bad in ("app/../x", "/app/x", "app\\x", "app/CON.txt", "app/a:b", "other/x"):
        with pytest.raises(SourceError):
            safe_parts(bad)
    with pytest.raises(ValueError):
        BuildParams(version="1.0.7.0", title="Shop")  # three parts, as Velopack wants
    with pytest.raises(ValueError):
        PackParams(version="1.0.7", title="Shop", pack_id="../Shop")
    monkeypatch.setenv("WIN_WORKER_TOKEN", "fww_secret")
    assert "WIN_WORKER_TOKEN" not in job_environment() and os.environ["WIN_WORKER_TOKEN"]


def test_a_release_must_be_what_it_says(tmp_path: Path) -> None:
    velopack_release(tmp_path, "Shop", "1.0.3", signed=False)
    files = {p.name: {"size": p.stat().st_size,
                      "sha256": hashlib.sha256(p.read_bytes()).hexdigest().upper()}
             for p in tmp_path.iterdir()}  # fmt: skip
    check_release(tmp_path, files, "Shop", "1.0.3", signed=False)
    with pytest.raises(ReleaseProblem, match="not signed"):
        check_release(tmp_path, files, "Shop", "1.0.3", signed=True)
    wrong = files | {"Shop-1.0.3-full.nupkg": {"size": 1, "sha256": "0" * 64}}
    with pytest.raises(ReleaseProblem, match="does not match"):
        check_release(tmp_path, wrong, "Shop", "1.0.3", signed=False)
    assert signature_size(tmp_path / "Shop-win-Setup.exe") == 0
    (tmp_path / "signed.exe").write_bytes(program(True))
    assert signature_size(tmp_path / "signed.exe") == 2048
    upload = tmp_path / "upload.zip"
    with zipfile.ZipFile(upload, "w") as archive:
        archive.writestr("../escape.exe", b"MZ")
    with pytest.raises(ReleaseProblem, match="should not have"):
        unpack_release(upload, tmp_path / "kept", "Shop", "1.0.3", False, 10**9)
