"""The host worker (W25a): releases are built in throwaway gVisor containers without secrets,
run only under gVisor, hardened and limited, with a network and a PostgreSQL per app; a release
that does not get healthy is removed and the previous one keeps running.

Docker is a stand-in that records every call, so these tests run everywhere."""

import asyncio
import json
import os
import sys
import tarfile
from pathlib import Path

import httpx
import pytest
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
from pydantic import ValidationError

from docker_standin import JOB_ID, FakeDocker, plan, release_job, runner_for, tarball
from forge_hostworker.cli import main
from forge_hostworker.client import HostClient
from forge_hostworker.wire import (
    HostJob,
    JobResult,
    SealedSecrets,
    ServicePlan,
    open_sealed,
    public_key,
    seal,
)


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
        ["network", "create", "--driver", "bridge", "--subnet", "10.89.0.0/24", "forge-build"],
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
