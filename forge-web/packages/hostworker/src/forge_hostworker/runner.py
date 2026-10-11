"""Running releases: build, start every service in a hardened gVisor container on the app's
network, wait until each is healthy, then switch; a release that does not get healthy is
removed and the previous one keeps running. The previous release stays (stopped) for a rollback.

On the host, per app and environment:
    <data>/apps/<app>-<environment>/releases/<n>/   the unpacked, built release
    <data>/apps/<app>-<environment>/state.json      {"live": n, "previous": m}
    <data>/apps/<app>-<environment>/db.json         the database password (only the worker)
"""

import asyncio
import os
import secrets
import shutil
import time
from collections.abc import Awaitable, Callable
from pathlib import Path

import httpx
from pydantic import BaseModel

from forge_hostworker.build import SourceError, build_release, hand_over, unpack
from forge_hostworker.docker import Docker, HostError, hardened, require_runsc
from forge_hostworker.firewall import BUILD_SUBNET
from forge_hostworker.postgres import ensure_database
from forge_hostworker.wire import DeployPlan, HostJob, JobResult, ServicePlan, ServiceState

HEALTH_TIMEOUT_S = 120
BUILD_NETWORK = "forge-build"
PATH = "/app/.venv/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
Health = Callable[[str, float], Awaitable[bool]]


class AppState(BaseModel):
    """Which releases of an app are live and kept."""

    live: int | None = None
    previous: int | None = None


async def http_health(url: str, timeout_s: float) -> bool:
    """Whether `url` answers below 500 within the time (asked every second)."""
    deadline = time.monotonic() + timeout_s
    async with httpx.AsyncClient(timeout=5) as client:
        while True:
            try:
                if (await client.get(url)).status_code < 500:
                    return True
            except httpx.HTTPError:
                pass
            if time.monotonic() > deadline:
                return False
            await asyncio.sleep(1)


def default_user() -> str:
    """The user containers run as: the worker's own (it owns the release folders)."""
    uid = os.getuid() if hasattr(os, "getuid") else 1000
    gid = os.getgid() if hasattr(os, "getgid") else 1000
    return "1000:1000" if uid == 0 else f"{uid}:{gid}"


def network(app: str, environment: str) -> str:
    """The app's own network: its services and its database, nothing else."""
    return f"forge-{app}-{environment}"


def service_container(app: str, environment: str, service: str, release: int) -> str:
    """A service container's name."""
    return f"forge-{app}-{environment}-{service}-r{release}"


class HostRunner:
    """Runs the jobs of one host."""

    def __init__(
        self,
        docker: Docker,
        data_dir: Path,
        *,
        user: str | None = None,
        health: Health = http_health,
        health_timeout_s: float = HEALTH_TIMEOUT_S,
        on_change: Callable[[], Awaitable[None]] | None = None,
        owner: tuple[int, int] | None = None,
    ) -> None:
        self.docker = docker
        self.data_dir = data_dir
        self.user = user or default_user()
        self.owner = owner  # the host uid:gid of `user` (None: the worker's own, nothing to do)
        self.health = health
        self.health_timeout_s = health_timeout_s
        self.on_change = on_change  # the edge reloads after every change of what runs

    def folder(self, app: str, environment: str) -> Path:
        """An app's folder on the host."""
        return self.data_dir / "apps" / f"{app}-{environment}"

    def state(self, app: str, environment: str) -> AppState:
        """Which releases are live and kept."""
        path = self.folder(app, environment) / "state.json"
        if not path.is_file():
            return AppState()
        return AppState.model_validate_json(path.read_text(encoding="utf-8"))

    def save(self, app: str, environment: str, state: AppState) -> None:
        """Remember which releases are live and kept."""
        path = self.folder(app, environment) / "state.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(state.model_dump_json(), encoding="utf-8")

    async def run(
        self, job: HostJob, source: Path | None, secret_values: dict[str, str]
    ) -> JobResult:
        """Run one job; expected failures are results, not exceptions."""
        started = time.monotonic()
        try:
            await require_runsc(self.docker)
            if job.kind == "release" and job.plan is not None and source is not None:
                result = await self.release(job.plan, source, secret_values)
            elif job.kind == "rollback":
                result = await self.rollback(job.app, job.environment)
            elif job.kind == "stop":
                result = await self.stop(job.app, job.environment)
            else:
                result = JobResult(
                    ok=False,
                    error=f"this host worker cannot run {job.kind} jobs",
                    hint="update forge-host-worker on this host",
                )
        except HostError as error:
            result = JobResult(ok=False, error=str(error), hint=error.hint)
        except SourceError as error:
            result = JobResult(ok=False, error=f"the source was refused: {error}")
        if result.ok and self.on_change is not None:
            await self.on_change()
        return result.model_copy(update={"seconds": round(time.monotonic() - started, 1)})

    async def release(
        self, plan: DeployPlan, source: Path, secret_values: dict[str, str]
    ) -> JobResult:
        """Build and start a release; switch to it only when every service is healthy."""
        state = self.state(plan.app, plan.environment)
        folder = self.folder(plan.app, plan.environment)
        release_dir = folder / "releases" / str(plan.release)
        await asyncio.to_thread(shutil.rmtree, release_dir, True)
        await asyncio.to_thread(unpack, source, release_dir)
        if self.owner is not None:
            await asyncio.to_thread(hand_over, release_dir, self.owner)
        await self.ensure_build_network()
        net = await self.ensure_network(plan)
        database_url = ""
        if plan.database:
            database_url = await ensure_database(
                self.docker, plan.app, plan.environment, net, folder, plan.limits.memory_mb
            )
        failed = await build_release(self.docker, plan, release_dir, self.user)
        if failed is not None:
            service, output = failed
            return JobResult(
                ok=False,
                release=state.live or 0,
                log_tail=output.tail(),
                error=f"the build of {service.name} failed",
            )
        started = await self.start_release(plan, release_dir, database_url, secret_values)
        unhealthy = [s.name for s in started if not s.healthy]
        if unhealthy:
            await self.remove([s.container for s in started])
            return JobResult(
                ok=False,
                release=state.live or 0,
                rolled_back=state.live is not None,
                error=f"{', '.join(unhealthy)} did not get healthy; the release was removed",
                log_tail=await self.logs(started),
            )
        self.keep_plan(plan)
        await self.switch(plan, state)
        return JobResult(ok=True, release=plan.release, services=started)

    async def ensure_build_network(self) -> None:
        """The network builds run on, in the subnet the firewall knows."""
        if (await self.docker("network", "inspect", BUILD_NETWORK)).code != 0:
            made = await self.docker(
                "network", "create", "--driver", "bridge", "--subnet", BUILD_SUBNET, BUILD_NETWORK
            )
            if made.code != 0:
                raise HostError(
                    f"the build network could not be made: {made.tail()[-300:]}",
                    hint="see the Docker daemon's log",
                )

    async def ensure_network(self, plan: DeployPlan) -> str:
        """The app's network, made once."""
        name = network(plan.app, plan.environment)
        if (await self.docker("network", "inspect", name)).code != 0:
            made = await self.docker(
                "network", "create", "--driver", "bridge", "--label", f"forge.app={plan.app}", name
            )
            if made.code != 0:
                raise HostError(
                    f"the app's network could not be made: {made.tail()[-300:]}",
                    hint="see the Docker daemon's log",
                )
        return name

    async def start_release(
        self, plan: DeployPlan, release_dir: Path, database_url: str, secret_values: dict[str, str]
    ) -> list[ServiceState]:
        """Start every service of the release and check its health."""
        states: list[ServiceState] = []
        for service in plan.services:
            name = service_container(plan.app, plan.environment, service.name, plan.release)
            await self.docker("rm", "-f", name)
            env = {**service.env, "PORT": str(service.port), "PATH": PATH, "HOME": "/tmp"}
            env |= {"DATABASE_URL": database_url} if database_url else {}
            env |= {key: secret_values[key] for key in service.secrets if key in secret_values}
            started = await self.start_container(plan, service, release_dir, name, env)
            healthy = started and await self.healthy(plan, service, name)
            states.append(ServiceState(name=service.name, container=name, healthy=healthy))
        return states

    async def start_container(
        self,
        plan: DeployPlan,
        service: ServicePlan,
        release_dir: Path,
        name: str,
        env: dict[str, str],
    ) -> bool:
        """`docker run` one service; the environment goes through a file only the worker reads."""
        env_file = self.data_dir / "tmp" / f"{secrets.token_hex(8)}.env"
        env_file.parent.mkdir(parents=True, exist_ok=True)
        env_file.touch(mode=0o600)
        env_file.write_text("".join(f"{k}={v}\n" for k, v in env.items()), encoding="utf-8")
        try:
            result = await self.docker(
                *run_args(plan, service, release_dir, name, env_file, self.user)
            )
        finally:
            env_file.unlink(missing_ok=True)
        return result.code == 0

    async def healthy(self, plan: DeployPlan, service: ServicePlan, name: str) -> bool:
        """Whether the service answers its health path on the app's network."""
        net = network(plan.app, plan.environment)
        template = f'{{{{(index .NetworkSettings.Networks "{net}").IPAddress}}}}'
        found = await self.docker("inspect", "-f", template, name)
        address = found.out.strip()
        if found.code != 0 or not address:
            return False
        return await self.health(
            f"http://{address}:{service.port}{service.health}", self.health_timeout_s
        )

    async def switch(self, plan: DeployPlan, state: AppState) -> None:
        """Make the new release live; stop the one before it, drop older ones."""
        old = state.previous
        if state.live is not None and state.live != plan.release:
            await self.stop_release(plan.app, plan.environment, state.live)
            state.previous = state.live
        state.live = plan.release
        self.save(plan.app, plan.environment, state)
        if old is not None and old not in (state.live, state.previous):
            await self.drop_release(plan.app, plan.environment, old)

    async def rollback(self, app: str, environment: str) -> JobResult:
        """Start the previous release again; the live one stops only when it is healthy."""
        state = self.state(app, environment)
        plan = self.saved_plan(app, environment, state.previous)
        if state.live is None or plan is None:
            return JobResult(
                ok=False, release=state.live or 0, error="there is no previous release"
            )
        services = []
        for service in plan.services:
            name = service_container(app, environment, service.name, plan.release)
            started = (await self.docker("start", name)).code == 0
            healthy = started and await self.healthy(plan, service, name)
            services.append(ServiceState(name=service.name, container=name, healthy=healthy))
        if not all(s.healthy for s in services):
            await self.stop_release(app, environment, plan.release)
            return JobResult(
                ok=False,
                release=state.live,
                services=services,
                error="the previous release did not get healthy; the live one stays",
            )
        await self.stop_release(app, environment, state.live)
        state.live, state.previous = plan.release, state.live
        self.save(app, environment, state)
        return JobResult(ok=True, release=plan.release, services=services, rolled_back=True)

    def saved_plan(self, app: str, environment: str, release: int | None) -> DeployPlan | None:
        """The plan a release was started with (kept for rollbacks)."""
        if release is None:
            return None
        path = self.folder(app, environment) / "plans" / f"{release}.json"
        return (
            DeployPlan.model_validate_json(path.read_text(encoding="utf-8"))
            if path.is_file()
            else None
        )

    def keep_plan(self, plan: DeployPlan) -> None:
        """Keep a release's plan next to it."""
        path = self.folder(plan.app, plan.environment) / "plans" / f"{plan.release}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(plan.model_dump_json(), encoding="utf-8")

    async def stop(self, app: str, environment: str) -> JobResult:
        """Stop every container of the app (it stays on the host; a release starts it again)."""
        listed = await self.docker(
            "ps",
            "-q",
            "--filter",
            f"label=forge.app={app}",
            "--filter",
            f"label=forge.env={environment}",
        )
        ids = listed.out.split()
        if ids:
            await self.docker("stop", *ids)
        return JobResult(ok=True, release=self.state(app, environment).live or 0)

    async def containers(self, app: str, environment: str, release: int) -> list[str]:
        """The service containers of one release."""
        listed = await self.docker(
            "ps",
            "-a",
            "--format",
            "{{.Names}}",
            "--filter",
            f"label=forge.app={app}",
            "--filter",
            f"label=forge.env={environment}",
            "--filter",
            f"label=forge.release={release}",
        )
        return listed.out.split()

    async def stop_release(self, app: str, environment: str, release: int) -> None:
        """Stop a release's containers but keep them (a rollback starts them again)."""
        names = await self.containers(app, environment, release)
        if names:
            await self.docker("stop", *names)

    async def drop_release(self, app: str, environment: str, release: int) -> None:
        """Remove a release's containers and files."""
        await self.remove(await self.containers(app, environment, release))
        folder = self.folder(app, environment)
        await asyncio.to_thread(shutil.rmtree, folder / "releases" / str(release), True)
        (folder / "plans" / f"{release}.json").unlink(missing_ok=True)

    async def remove(self, names: list[str]) -> None:
        """Remove containers."""
        if names:
            await self.docker("rm", "-f", *names)

    async def logs(self, started: list[ServiceState]) -> str:
        """The end of the logs of services that did not get healthy."""
        parts = []
        for service in started:
            if not service.healthy:
                found = await self.docker("logs", "--tail", "100", service.container)
                parts.append(f"--- {service.name}\n{found.tail()[-4000:]}")
        return "\n".join(parts)[-20_000:]


def run_args(
    plan: DeployPlan, service: ServicePlan, release_dir: Path, name: str, env_file: Path, user: str
) -> list[str]:
    """`docker run` for one service: gVisor, read-only, no capabilities, limits, its network."""
    if service.runtime == "static":  # the runtime image serves /srv; the build's output is it
        mount = ["-v", f"{release_dir / service.root / service.output}:/srv:ro"]
    else:
        mount = ["-v", f"{release_dir / service.root}:/app:ro", "-w", "/app"]
    labels = [
        "--label",
        f"forge.app={plan.app}",
        "--label",
        f"forge.env={plan.environment}",
        "--label",
        f"forge.release={plan.release}",
        "--label",
        f"forge.service={service.name}",
        "--label",
        f"forge.port={service.port}",
        "--label",
        f"forge.route={service.route or ''}",
    ]
    limits = plan.limits
    return [
        "run",
        "-d",
        "--name",
        name,
        *hardened(user),
        "--read-only",
        "--tmpfs",
        "/tmp:rw,size=256m",
        "--restart",
        "unless-stopped",
        "--network",
        network(plan.app, plan.environment),
        "--memory",
        f"{limits.memory_mb}m",
        "--cpus",
        str(limits.cpus),
        "--pids-limit",
        str(limits.pids),
        "--env-file",
        str(env_file),
        *mount,
        *labels,
        service.image,
        *service.command,
    ]
