"""Windows builds (W38a): a job goes from the server to a Windows worker (the real
`forge-win-worker` client, over HTTP) and back: the approved commit is packed, the app's tests
run, the package is sent and kept, the version rises with every build, a failing test fails the
build, and a worker only ever sees its own running jobs. dotnet is a stand-in that writes what
`dotnet test` and `dotnet publish` would."""

import asyncio
import contextlib
import shutil
import tarfile
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest

from forge_web.stores import windows_api
from forge_winworker.build import counted, set_version, unpack
from forge_winworker.client import WorkerClient
from forge_winworker.runners import DirectRunner
from forge_winworker.wire import JobOffer, MsixParams
from support import LiveServer
from test_hosting_deploy import app_project, approve_head, git, settings_for, short_polls, until

__all__ = ["short_polls"]
MANIFEST = '<Package><Identity Name="Shop" Publisher="CN=Shop" Version="1.0.0.0" /></Package>\n'
WINDOWS = {"windows/App/App.csproj": "<Project />\n", "windows/Tests/Tests.csproj": "<Project />\n",
           "windows/App/Package.appxmanifest": MANIFEST}  # fmt: skip
TRX = (
    '<TestRun xmlns="http://microsoft.com/schemas/VisualStudio/TeamTest/2010"><ResultSummary>'
    '<Counters total="{n}" passed="{passed}" failed="{failed}" /></ResultSummary></TestRun>'
)


class FakeDotnet(DirectRunner):
    """Runs no dotnet: `test` writes a TRX file, `publish` an .msixupload, as dotnet would."""

    def __init__(self, work: Path) -> None:
        super().__init__(work)
        self.failing = False
        self.versions: list[str] = []  # the manifest's version at each publish
        self.commands: list[list[str]] = []

    async def run(self, command: list[str], cwd: Path) -> tuple[int, str]:
        self.commands.append(command)
        if command[:2] == ["dotnet", "test"]:
            results = Path(command[command.index("--results-directory") + 1])
            results.mkdir(parents=True, exist_ok=True)
            failed = 1 if self.failing else 0
            (results / "run.trx").write_text(TRX.format(n=3, passed=3 - failed, failed=failed))
            return (1, "Failed AccountTests.SignIn") if self.failing else (0, "Passed!")
        out = Path(next(a for a in command if a.startswith("-p:AppxPackageDir=")).split("=", 1)[1])
        manifest = (cwd / "windows/App/Package.appxmanifest").read_text(encoding="utf-8")
        self.versions.append(manifest.split('Version="')[1].split('"')[0])
        made = out / "Shop_x64_Test" / f"Shop_{self.versions[-1]}_x64.msixupload"
        made.parent.mkdir(parents=True, exist_ok=True)
        made.write_bytes(b"PK\x03\x04 an msixupload " + self.versions[-1].encode())
        return 0, "Build succeeded."


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


async def worker_token(client: httpx.AsyncClient, name: str = "win-1") -> str:
    made = await client.post("/api/admin/stores/windows/workers", json={"name": name})
    assert made.status_code == 200, made.text
    return str(made.json()["token"])


@contextlib.asynccontextmanager
async def worker_online(url: str, token: str, runner: FakeDotnet) -> AsyncIterator[None]:
    """The real worker client, taking jobs while the block runs."""
    worker = WorkerClient(url, token, runner)
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


async def test_a_job_goes_to_the_windows_worker_and_back(
    server: LiveServer, client: httpx.AsyncClient, tmp_path: Path
) -> None:
    runner = FakeDotnet(tmp_path / "worker")
    async with worker_online(server.url, await worker_token(client), runner):
        project_id = await windows_project(server, client)
        first = await started(client, project_id)
        done = await until(lambda: finished(client, project_id, first["id"]))
        assert done["status"] == "built", done["error"]
        assert (done["version"], done["tests_passed"]) == ("1.0.1.0", 3)
        assert done["file_name"] == "Shop_1.0.1.0_x64.msixupload"
        url = f"/api/projects/{project_id}/windows/builds/{first['id']}/package"
        package = await client.get(url)
        assert package.status_code == 200 and package.content.endswith(b"1.0.1.0")
        second = await started(client, project_id)
        again = await until(lambda: finished(client, project_id, second["id"]))
        assert again["version"] == "1.0.2.0" and runner.versions == ["1.0.1.0", "1.0.2.0"]
    publish = next(c for c in runner.commands if c[:2] == ["dotnet", "publish"])
    assert "-p:UapAppxPackageBuildMode=StoreUpload" in publish
    assert "-p:AppxPackageSigningEnabled=false" in publish  # the Store signs it
    assert not list((tmp_path / "worker" / "jobs").iterdir())  # nothing stays on the machine


async def test_a_failing_test_or_a_missing_app_fails_the_build(
    server: LiveServer, client: httpx.AsyncClient, tmp_path: Path
) -> None:
    runner = FakeDotnet(tmp_path / "worker")
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
    build = await started(client, project_id)
    async with httpx.AsyncClient(base_url=server.url, timeout=30) as raw:

        async def as_worker(token: str, method: str, path: str, **kwargs: Any) -> httpx.Response:
            headers = {"Authorization": f"Bearer {token}"}
            return await raw.request(method, path, headers=headers, **kwargs)

        assert (await as_worker("fww_x_y", "POST", "/api/win/poll",
                                json={"free_slots": 1})).status_code == 401  # fmt: skip
        polled = await until(lambda: poll_job(as_worker, mine))
        offer = JobOffer.model_validate(polled)
        assert offer.id == build["id"] and offer.msix == MsixParams(version="1.0.1.0")
        assert set(polled) == {"id", "kind", "project", "msix", "timeout_s"}  # no secrets
        source = f"/api/win/jobs/{offer.id}/source"
        assert (await as_worker(other, "GET", source)).status_code == 409
        assert (await as_worker(mine, "GET", source)).status_code == 200
        result = f"/api/win/jobs/{offer.id}/result"
        late = await as_worker(other, "POST", result, content=b'{"ok": true}')
        assert late.status_code == 409
    rows = (await client.get("/api/admin/stores/windows/workers")).json()
    assert {r["name"] for r in rows} == {"mine", "other"} and "token" not in rows[0]


async def poll_job(as_worker: Any, token: str) -> dict[str, Any] | None:
    answer = await as_worker(token, "POST", "/api/win/poll", json={"free_slots": 1})
    assert answer.status_code == 200, answer.text
    job = answer.json()["job"]
    return dict(job) if job else None


def test_the_build_steps_on_their_own(tmp_path: Path) -> None:
    archive = tmp_path / "source.tar.gz"
    with tarfile.open(archive, "w:gz") as tar:
        manifest = tmp_path / "m"
        manifest.write_text(MANIFEST, encoding="utf-8")
        tar.add(manifest, "windows/App/Package.appxmanifest")
    unpack(archive, tmp_path / "src")
    target = tmp_path / "src" / "windows/App/Package.appxmanifest"
    set_version(target, "1.0.7.0")
    assert 'Version="1.0.7.0"' in target.read_text(encoding="utf-8")
    results = tmp_path / "results"
    results.mkdir()
    (results / "a.trx").write_text(TRX.format(n=5, passed=4, failed=1))
    assert counted(results) == (4, 1)
    with pytest.raises(ValueError):
        MsixParams(version="1.0.7.1")  # the Store wants the last part 0
    shutil.rmtree(tmp_path / "src")
