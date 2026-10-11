"""The host worker (W25a): releases are built in throwaway gVisor containers without secrets,
run only under gVisor, hardened and limited, with a network and a PostgreSQL per app; a release
that does not get healthy is removed and the previous one keeps running.

Docker is a stand-in that records every call, so these tests run everywhere."""

import asyncio
import io
import json
import os
import sys
import tarfile
from pathlib import Path
from typing import Any

import httpx
import pytest
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
from pydantic import ValidationError

from forge_hostworker.cli import main
from forge_hostworker.client import HostClient
from forge_hostworker.docker import DockerResult
from forge_hostworker.runner import HostRunner
from forge_hostworker.wire import (
    DeployPlan,
    HostJob,
    JobResult,
    SealedSecrets,
    ServicePlan,
    open_sealed,
    public_key,
    seal,
)

JOB_ID = "a" * 32


class FakeDocker:
    """Docker that runs nothing: it remembers containers, networks and every call."""

    def __init__(self, runtimes: tuple[str, ...] = ("runc", "runsc")) -> None:
        self.runtimes = runtimes
        self.calls: list[list[str]] = []
        self.env_files: dict[str, str] = {}  # container -> its --env-file, read at `run`
        self.containers: dict[str, dict[str, Any]] = {}
        self.networks: set[str] = set()
        self.fail: set[str] = set()  # images whose `run` fails (a broken build)

    async def __call__(self, *args: str, timeout: float = 600) -> DockerResult:
        self.calls.append(list(args))
        handler = getattr(self, f"do_{args[0]}", None)
        return handler(list(args[1:])) if handler else DockerResult(0, "", "")

    def do_info(self, args: list[str]) -> DockerResult:
        return DockerResult(0, json.dumps({name: {} for name in self.runtimes}), "")

    def do_network(self, args: list[str]) -> DockerResult:
        if args[0] == "inspect":
            return DockerResult(0 if args[1] in self.networks else 1, "", "")
        self.networks.add(args[-1])
        return DockerResult(0, "", "")

    def do_container(self, args: list[str]) -> DockerResult:
        found = self.containers.get(args[-1])
        return (
            DockerResult(1, "", "no such container")
            if found is None
            else DockerResult(0, "true" if found["running"] else "false", "")
        )

    def do_run(self, args: list[str]) -> DockerResult:
        image = next(a for a in args if a.startswith(("runtime/", "postgres:")))
        if image in self.fail:
            return DockerResult(1, "", "npm ERR! build failed")
        if "--rm" in args:
            return DockerResult(0, "built", "")
        name = args[args.index("--name") + 1]
        labels = dict(a.split("=", 1) for i, a in enumerate(args) if args[i - 1] == "--label")
        if "--env-file" in args:
            self.env_files[name] = Path(args[args.index("--env-file") + 1]).read_text()
        self.containers[name] = {"labels": labels, "running": True}
        return DockerResult(0, "c0ffee", "")

    def do_inspect(self, args: list[str]) -> DockerResult:
        return (
            DockerResult(0, f"10.0.0.{len(args[-1])}", "")
            if args[-1] in self.containers
            else (DockerResult(1, "", ""))
        )

    def do_rm(self, args: list[str]) -> DockerResult:
        for name in args[1:]:
            self.containers.pop(name, None)
        return DockerResult(0, "", "")

    def do_start(self, args: list[str]) -> DockerResult:
        for name in args:
            self.containers[name]["running"] = True
        return DockerResult(0, "", "")

    def do_stop(self, args: list[str]) -> DockerResult:
        for name in args:
            self.containers[name]["running"] = False
        return DockerResult(0, "", "")

    def do_ps(self, args: list[str]) -> DockerResult:
        wanted = dict(
            a.removeprefix("label=").split("=", 1)
            for i, a in enumerate(args)
            if args[i - 1] == "--filter"
        )
        names = [
            n
            for n, c in self.containers.items()
            if all(c["labels"].get(k) == v for k, v in wanted.items())
            and ("-a" in args or c["running"])
        ]
        return DockerResult(0, "\n".join(names), "")

    def runs(self) -> list[list[str]]:
        """The `docker run` calls."""
        return [c for c in self.calls if c[0] == "run"]


def plan(release: int = 1, **changes: Any) -> DeployPlan:
    api = ServicePlan(
        name="api",
        runtime="python3.12",
        image="runtime/python:3.12",
        root="server",
        build=["uv", "sync", "--locked", "--no-dev"],
        command=["uvicorn", "app.main:create_app", "--factory"],
        port=8000,
        health="/healthz",
        route="/api",
        env={"LOG_LEVEL": "info"},
        secrets=["MAPS_KEY"],
    )
    web = ServicePlan(
        name="web",
        runtime="static",
        image="runtime/static:1",
        root="web",
        build=["sh", "-c", "npm ci && npm run build"],
        port=8080,
        health="/",
        route="/",
    )
    fields: dict[str, Any] = {
        "app": "shop",
        "environment": "staging",
        "release": release,
        "services": [api, web],
        "database": True,
    } | changes
    return DeployPlan(**fields)


def release_job(release: int = 1, **changes: Any) -> HostJob:
    shown = plan(release, **changes)
    return HostJob(id=JOB_ID, kind="release", app="shop", environment="staging", plan=shown)


def tarball(path: Path, extra: dict[str, bytes] | None = None) -> Path:
    """A packed release with a server and a built web client."""
    files = {"server/app/main.py": b"app = 1\n", "web/dist/index.html": b"<html></html>"}
    with tarfile.open(path, "w:gz") as tar:
        for name, data in (files | (extra or {})).items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return path


def runner_for(tmp_path: Path, docker: FakeDocker, unhealthy: set[str] | None = None) -> HostRunner:
    async def health(url: str, timeout_s: float) -> bool:
        return not any(port in url for port in unhealthy or set())

    return HostRunner(docker, tmp_path / "host", user="1500:1500", health=health)


async def test_a_release_runs_every_service_under_gvisor_hardened_and_limited(
    tmp_path: Path,
) -> None:
    docker = FakeDocker()
    runner = runner_for(tmp_path, docker)
    result = await runner.run(release_job(), tarball(tmp_path / "s.tgz"), {"MAPS_KEY": "s3cr3t"})
    assert result.ok and result.release == 1, result
    assert [(s.name, s.healthy) for s in result.services] == [("api", True), ("web", True)]
    for call in docker.runs():
        assert "--runtime=runsc" in call
        assert call[call.index("--security-opt") + 1] == "no-new-privileges"
        assert "--read-only" in call
    apps = [c for c in docker.runs() if "-d" in c and "forge.role=database" not in c]
    for call in apps:
        assert call[call.index("--cap-drop") + 1] == "ALL" and "--cap-add" not in call
        assert call[call.index("--user") + 1] == "1500:1500"
        assert {"--memory", "--cpus", "--pids-limit"} <= set(call)
        assert call[call.index("--network") + 1] == "forge-shop-staging"
    api = docker.env_files["forge-shop-staging-api-r1"]
    assert "PORT=8000\n" in api and "MAPS_KEY=s3cr3t\n" in api and "LOG_LEVEL=info\n" in api
    assert "DATABASE_URL=postgresql://app:" in api and "@forge-shop-staging-db:5432/app" in api
    web = next(c for c in apps if "forge.service=web" in c)
    assert web[web.index("-v") + 1].endswith(f"{os.sep}web{os.sep}dist:/srv:ro")
    assert not list((tmp_path / "host" / "tmp").iterdir())  # env files are gone again
    assert all(c[0] not in ("build", "buildx", "compose") for c in docker.calls)
    state = json.loads((tmp_path / "host" / "apps" / "shop-staging" / "state.json").read_text())
    assert state == {"live": 1, "previous": None}


async def test_builds_get_no_secrets_and_only_the_build_network(tmp_path: Path) -> None:
    docker = FakeDocker()
    await runner_for(tmp_path, docker).run(
        release_job(), tarball(tmp_path / "s.tgz"), {"MAPS_KEY": "s3cr3t"}
    )
    builds = [c for c in docker.runs() if "--rm" in c]
    assert len(builds) == 2
    for call in builds:
        assert "--env-file" not in call and "s3cr3t" not in " ".join(call)
        assert call[call.index("--network") + 1] == "forge-build"
        assert [a for i, a in enumerate(call) if call[i - 1] == "-e"] == ["HOME=/tmp"]


async def test_a_failed_build_starts_nothing(tmp_path: Path) -> None:
    docker = FakeDocker()
    docker.fail.add("runtime/static:1")
    result = await runner_for(tmp_path, docker).run(release_job(), tarball(tmp_path / "s.tgz"), {})
    assert not result.ok and result.error == "the build of web failed"
    assert "npm ERR! build failed" in result.log_tail
    assert not [c for c in docker.runs() if "forge.role=database" not in c and "-d" in c]


async def test_an_unhealthy_release_is_removed_and_the_previous_one_keeps_running(
    tmp_path: Path,
) -> None:
    docker = FakeDocker()
    good = runner_for(tmp_path, docker)
    assert (await good.run(release_job(1), tarball(tmp_path / "1.tgz"), {})).ok
    bad = runner_for(tmp_path, docker, unhealthy={":8000"})
    result = await bad.run(release_job(2), tarball(tmp_path / "2.tgz"), {})
    assert not result.ok and result.rolled_back and result.release == 1
    assert "api did not get healthy" in result.error
    assert "forge-shop-staging-api-r2" not in docker.containers
    assert docker.containers["forge-shop-staging-api-r1"]["running"]
    assert good.state("shop", "staging").live == 1


async def test_a_new_release_stops_the_one_before_and_a_rollback_brings_it_back(
    tmp_path: Path,
) -> None:
    docker = FakeDocker()
    runner = runner_for(tmp_path, docker)
    for release in (1, 2):
        assert (await runner.run(release_job(release), tarball(tmp_path / f"{release}.tgz"), {})).ok
    assert not docker.containers["forge-shop-staging-api-r1"]["running"]  # kept, stopped
    assert docker.containers["forge-shop-staging-api-r2"]["running"]
    back = HostJob(id=JOB_ID, kind="rollback", app="shop", environment="staging")
    result = await runner.run(back, None, {})
    assert result.ok and result.rolled_back and result.release == 1
    assert docker.containers["forge-shop-staging-api-r1"]["running"]
    assert not docker.containers["forge-shop-staging-api-r2"]["running"]
    assert runner.state("shop", "staging").model_dump() == {"live": 1, "previous": 2}


async def test_older_releases_are_dropped(tmp_path: Path) -> None:
    docker = FakeDocker()
    runner = runner_for(tmp_path, docker)
    for release in (1, 2, 3):
        assert (await runner.run(release_job(release), tarball(tmp_path / f"{release}.tgz"), {})).ok
    assert "forge-shop-staging-api-r1" not in docker.containers
    releases = tmp_path / "host" / "apps" / "shop-staging" / "releases"
    assert sorted(p.name for p in releases.iterdir()) == ["2", "3"]


async def test_one_network_and_one_database_per_app(tmp_path: Path) -> None:
    docker = FakeDocker()
    runner = runner_for(tmp_path, docker)
    for release in (1, 2):
        assert (await runner.run(release_job(release), tarball(tmp_path / f"{release}.tgz"), {})).ok
    assert [c for c in docker.calls if c[:2] == ["network", "create"]] == [
        [
            "network",
            "create",
            "--driver",
            "bridge",
            "--label",
            "forge.app=shop",
            "forge-shop-staging",
        ],
    ]
    databases = [c for c in docker.runs() if "forge.role=database" in c]
    assert (
        len(databases) == 1
        and "forge-shop-staging-db-data:/var/lib/postgresql/data" in (databases[0])
    )
    url = [
        line
        for line in docker.env_files["forge-shop-staging-api-r1"].splitlines()
        if line.startswith("DATABASE_URL=")
    ]
    assert url and url[0] in docker.env_files["forge-shop-staging-api-r2"]
    password = tmp_path / "host" / "apps" / "shop-staging" / "db.json"
    if sys.platform != "win32":
        assert password.stat().st_mode & 0o777 == 0o600


async def test_without_gvisor_nothing_runs(tmp_path: Path) -> None:
    docker = FakeDocker(runtimes=("runc",))
    result = await runner_for(tmp_path, docker).run(release_job(), tarball(tmp_path / "s.tgz"), {})
    assert not result.ok and "gVisor (runsc)" in result.error and "runsc install" in result.hint
    assert [c[0] for c in docker.calls] == ["info"]


async def test_sources_that_leave_their_folder_are_refused(tmp_path: Path) -> None:
    for name in ("../escape.txt", "/etc/cron.d/x"):
        docker = FakeDocker()
        source = tarball(tmp_path / "evil.tgz", {name: b"x"})
        result = await runner_for(tmp_path, docker).run(release_job(), source, {})
        assert not result.ok and "refused" in result.error, name
    linked = tmp_path / "link.tgz"
    with tarfile.open(linked, "w:gz") as tar:
        info = tarfile.TarInfo("server/link")
        info.type, info.linkname = tarfile.SYMTYPE, "/etc/passwd"
        tar.addfile(info)
    result = await runner_for(tmp_path, FakeDocker()).run(release_job(), linked, {})
    assert not result.ok and "links" in result.error


def test_plans_hold_names_not_values() -> None:
    base = {
        "name": "api",
        "runtime": "python3.12",
        "image": "runtime/python:3.12",
        "command": ["x"],
        "port": 8000,
    }
    for wrong in (
        {"secrets": ["sk_live_123"]},
        {"env": {"PORT": "1"}},
        {"root": "../x"},
        {"root": "/etc"},
        {"image": "Bad Image"},
        {"command": []},
        {"runtime": "ruby"},
        {"health": "healthz"},
    ):
        with pytest.raises(ValidationError):
            ServicePlan(**(base | wrong))
    assert (
        ServicePlan(name="web", runtime="static", image="runtime/static:1", port=8080).command == []
    )
    with pytest.raises(ValidationError):  # the plan of another app
        HostJob(id=JOB_ID, kind="release", app="other", environment="staging", plan=plan())
    with pytest.raises(ValidationError):
        HostJob(id=JOB_ID, kind="stop", app="shop", environment="staging", plan=plan())
    with pytest.raises(ValidationError):
        plan(services=[plan().services[0], plan().services[0]])


def test_sealed_secrets_open_only_with_the_hosts_key() -> None:
    host = X25519PrivateKey.generate()
    box = seal(public_key(host), b'{"MAPS_KEY": "s3cr3t"}')
    assert "s3cr3t" not in box.model_dump_json()
    assert json.loads(open_sealed(host, box)) == {"MAPS_KEY": "s3cr3t"}
    with pytest.raises(InvalidTag):
        open_sealed(X25519PrivateKey.generate(), box)
    tampered = box.model_copy(update={"sealed": box.sealed[:-4] + "AAAA"})
    with pytest.raises(InvalidTag):
        open_sealed(host, tampered)


async def test_the_client_runs_a_job_from_the_server(tmp_path: Path) -> None:
    host = X25519PrivateKey.generate()
    source = tarball(tmp_path / "s.tgz").read_bytes()
    sealed = seal(public_key(host), json.dumps({"MAPS_KEY": "s3cr3t"}).encode())
    offered = [release_job()]
    reported: list[JobResult] = []
    stop = asyncio.Event()
    secret_fetches = []

    def server(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == "Bearer fhw_token"
        path = request.url.path
        if path == "/api/host/poll":
            job = offered.pop().model_dump(mode="json") if offered else None
            return httpx.Response(200, json={"job": job})
        if path == f"/api/host/jobs/{JOB_ID}/source":
            return httpx.Response(200, content=source)
        if path == f"/api/host/jobs/{JOB_ID}/secrets":
            secret_fetches.append(path)
            return httpx.Response(200, content=sealed.model_dump_json())
        if path == f"/api/host/jobs/{JOB_ID}/result":
            reported.append(JobResult.model_validate_json(request.content))
            stop.set()
            return httpx.Response(204)
        return httpx.Response(404)

    docker = FakeDocker()
    client = HostClient(
        "https://forge.example",
        "fhw_token",
        runner_for(tmp_path, docker),
        host,
        transport=httpx.MockTransport(server),
    )
    await asyncio.wait_for(client.serve(stop), 10)
    assert reported and reported[0].ok and reported[0].release == 1
    assert len(secret_fetches) == 1
    assert "MAPS_KEY=s3cr3t" in docker.env_files["forge-shop-staging-api-r1"]


async def test_wrongly_sealed_secrets_fail_the_job_and_say_so(tmp_path: Path) -> None:
    other = X25519PrivateKey.generate()
    sealed = seal(public_key(other), b"{}")
    client = HostClient(
        "https://forge.example",
        "fhw_token",
        runner_for(tmp_path, FakeDocker()),
        X25519PrivateKey.generate(),
        transport=httpx.MockTransport(
            lambda r: httpx.Response(200, content=sealed.model_dump_json())
        ),
    )
    with pytest.raises(InvalidTag):
        await client.secrets(JOB_ID)
    assert isinstance(SealedSecrets.model_validate_json(sealed.model_dump_json()), SealedSecrets)


def test_the_command_line_needs_a_token_a_key_and_gvisor(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("FORGE_HOST_TOKEN", raising=False)
    data = tmp_path / "data"
    run = ["run", "--server", "https://forge.example", "--data", str(data)]
    assert main(run) == 2 and "no host token" in capsys.readouterr().err
    data.mkdir()
    (data / "token").write_text("fhw_abc\n")
    assert main(run) == 2 and "keygen" in capsys.readouterr().err
    assert main(["keygen", "--data", str(data)]) == 0
    printed = capsys.readouterr().out
    assert "register this public key" in printed
    if sys.platform != "win32":
        assert (data / "host.key").stat().st_mode & 0o777 == 0o600
        fake = tmp_path / "docker"
        fake.write_text(f"#!{sys.executable}\nprint('{{\"runc\": {{}}}}')\n")
        fake.chmod(0o755)
        assert main([*run, "--docker", str(fake)]) == 2
        assert "gVisor (runsc) is not a Docker runtime" in capsys.readouterr().err
