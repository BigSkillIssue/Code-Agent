"""Docker for the host worker's tests: it runs nothing, remembers containers and networks and
records every call; plus a release plan, its job and its packed source."""

import io
import json
import tarfile
from pathlib import Path
from typing import Any

from forge_hostworker.docker import DockerResult
from forge_hostworker.runner import HostRunner
from forge_hostworker.wire import PLANNED, DeployPlan, HostJob, ServicePlan

JOB_ID = "a" * 32


class FakeDocker:
    """Docker that runs nothing: it remembers containers, networks and every call."""

    def __init__(self, runtimes: tuple[str, ...] = ("runc", "runsc"), userns: bool = False) -> None:
        self.runtimes = runtimes
        self.userns = userns
        self.subnets: dict[str, str] = {}
        self.volumes: set[str] = set()
        self.failing: set[str] = set()  # commands whose `run` fails (failing tests, say)
        self.once_env_files: list[str] = []  # the --env-file of each throwaway (`--rm`) run
        self.calls: list[list[str]] = []
        self.env_files: dict[str, str] = {}  # container -> its --env-file, read at `run`
        self.containers: dict[str, dict[str, Any]] = {}
        self.networks: set[str] = set()
        self.fail: set[str] = set()  # images whose `run` fails (a broken build)

    async def __call__(
        self, *args: str, timeout: float = 600, out: Path | None = None
    ) -> DockerResult:
        self.calls.append(list(args))
        handler = getattr(self, f"do_{args[0]}", None)
        result = handler(list(args[1:])) if handler else DockerResult(0, "", "")
        if out is not None and result.code == 0:
            out.write_bytes(b"PGDMP a dump of " + args[1].encode())
        return result

    def do_exec(self, args: list[str]) -> DockerResult:
        return DockerResult(0, "", "")

    def do_volume(self, args: list[str]) -> DockerResult:
        if args[0] == "inspect":
            return DockerResult(0 if args[1] in self.volumes else 1, "", "")
        if args[0] == "rm":
            self.volumes.discard(args[-1])
        else:
            self.volumes.add(args[-1])
        return DockerResult(0, "", "")

    def do_info(self, args: list[str]) -> DockerResult:
        if "{{json .SecurityOptions}}" in args:
            options = ["name=seccomp,profile=builtin", *(["name=userns"] if self.userns else [])]
            return DockerResult(0, json.dumps(options), "")
        return DockerResult(0, json.dumps({name: {} for name in self.runtimes}), "")

    def do_network(self, args: list[str]) -> DockerResult:
        if args[0] == "inspect":
            found = args[1] in self.networks
            return DockerResult(0 if found else 1, self.subnets.get(args[1], ""), "")
        if args[0] == "rm":
            self.networks.discard(args[-1])
            return DockerResult(0, "", "")
        self.networks.add(args[-1])
        if "--subnet" in args:
            self.subnets[args[-1]] = args[args.index("--subnet") + 1]
        return DockerResult(0, "", "")

    def do_container(self, args: list[str]) -> DockerResult:
        found = self.containers.get(args[-1])
        return (
            DockerResult(1, "", "no such container")
            if found is None
            else DockerResult(0, "true" if found["running"] else "false", "")
        )

    def do_run(self, args: list[str]) -> DockerResult:
        image = next(a for a in args if a.startswith(("runtime/", "postgres:", "mirror.gcr.io/")))
        if image in self.fail:
            return DockerResult(1, "", "npm ERR! build failed")
        if self.failing & set(args):
            return DockerResult(1, "", "FAILED tests/test_auth.py::test_sign_in - 1 failed")
        if "--rm" in args:
            if "--env-file" in args:
                self.once_env_files.append(Path(args[args.index("--env-file") + 1]).read_text())
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
        check_build=["uv", "sync", "--locked"],
        test=["pytest", "-q"],
        migrate=["alembic", "upgrade", "head"],
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
        test=["npm", "test"],
    )
    fields: dict[str, Any] = {
        "app": "shop",
        "environment": "staging",
        "release": release,
        "services": [api, web],
        "database": True,
    } | changes
    return DeployPlan(**fields)


def job_of(kind: str, release: int = 1, **changes: Any) -> HostJob:
    """A job of this kind for `shop` in staging (with the plan, when the kind carries one)."""
    shown = plan(release, **changes) if kind in PLANNED else None
    return HostJob(id=JOB_ID, kind=kind, app="shop", environment="staging", plan=shown)


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
