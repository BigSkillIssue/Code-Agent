"""The host's own checks of a release (W26b): results from the sandbox are not trusted, so before
anyone is asked to approve, the host builds the commit itself (with its dev dependencies, on
the build network) and runs every service's migrations and tests against a throwaway
PostgreSQL, on a throwaway network that reaches nothing else. Nothing of it stays."""

import asyncio
import secrets
import shutil
from pathlib import Path

from forge_hostworker.build import build_release, hand_over, unpack
from forge_hostworker.docker import Docker, hardened
from forge_hostworker.postgres import throwaway_database
from forge_hostworker.wire import CheckState, DeployPlan, JobResult, ServicePlan

PATH = ":".join(("/app/.venv/bin", "/app/node_modules/.bin", "/usr/local/sbin", "/usr/local/bin",
                 "/usr/sbin", "/usr/bin", "/sbin", "/bin"))  # fmt: skip
STEP_TIMEOUT_S = 1800
DETAIL_CHARS = 1500


def with_dev_builds(plan: DeployPlan) -> DeployPlan:
    """The plan with each service's check build (with dev dependencies) as its build."""
    services = [
        s.model_copy(update={"build": s.check_build}) if s.check_build is not None else s
        for s in plan.services
    ]
    return plan.model_copy(update={"services": services})


def step_args(plan: DeployPlan, service: ServicePlan, folder: Path, network: str, url: str,
              user: str) -> list[str]:  # fmt: skip
    """`docker run` for one migration or test run of a check."""
    env = {**service.env, "PORT": str(service.port), "PATH": PATH, "HOME": "/tmp"}
    env |= {"DATABASE_URL": url, "TEST_DATABASE_URL": url} if url else {}  # as `forge app check`
    return [
        "run", "--rm", *hardened(user), "--read-only", "--tmpfs", "/tmp:rw,exec,size=1024m",
        "--tmpfs", "/data:rw,size=256m", "--network", network, "--memory", "2048m",
        "--cpus", "2", "--pids-limit", "1024", "-v", f"{folder / service.root}:/app", "-w", "/app",
        *[arg for key, value in env.items() for arg in ("-e", f"{key}={value}")],
        "--label", f"forge.app={plan.app}", "--label", "forge.role=check", service.image,
    ]  # fmt: skip


class Check:
    """One check job: its folder, network and database, all removed at the end."""

    def __init__(self, docker: Docker, root: Path, user: str, owner: tuple[int, int] | None):
        token = secrets.token_hex(6)
        self.docker, self.user, self.owner = docker, user, owner
        self.folder = root / "checks" / token
        self.network = f"forge-check-{token}"
        self.database = f"forge-check-{token}-db"

    async def run(self, plan: DeployPlan, source: Path) -> JobResult:
        """Build, then migrations and tests of every service."""
        try:
            return await self.checked(plan, source)
        finally:
            await self.docker("rm", "-f", self.database)
            await self.docker("network", "rm", self.network)
            await asyncio.to_thread(shutil.rmtree, self.folder, True)

    async def checked(self, plan: DeployPlan, source: Path) -> JobResult:
        await asyncio.to_thread(unpack, source, self.folder)
        if self.owner is not None:
            await asyncio.to_thread(hand_over, self.folder, self.owner)
        failed = await build_release(self.docker, with_dev_builds(plan), self.folder, self.user)
        if failed is not None:
            service, output = failed
            state = CheckState(name=f"{service.name}: build", ok=False,
                               detail=output.tail()[-DETAIL_CHARS:])  # fmt: skip
            return JobResult(ok=False, checks=[state], log_tail=output.tail(),
                             error=f"the build of {service.name} failed")  # fmt: skip
        await self.docker("network", "create", "--internal", "--label", f"forge.app={plan.app}",
                          self.network)  # fmt: skip
        url = ""
        if plan.database:
            url = await throwaway_database(self.docker, self.database, self.network, plan.app,
                                           self.folder.parent)  # fmt: skip
        return await self.steps(plan, url)

    async def steps(self, plan: DeployPlan, url: str) -> JobResult:
        states: list[CheckState] = []
        logs: list[str] = []
        for service in plan.services:
            for label, command in (("migrations", service.migrate), ("tests", service.test)):
                if not command or (label == "migrations" and not url):
                    continue
                args = step_args(plan, service, self.folder, self.network, url, self.user)
                done = await self.docker(*args, *command, timeout=STEP_TIMEOUT_S)
                tail = done.tail()
                states.append(CheckState(name=f"{service.name}: {label}", ok=done.code == 0,
                                         detail=tail[-DETAIL_CHARS:]))  # fmt: skip
                logs.append(f"--- {service.name}: {label}\n{tail[-6000:]}")
        failing = [state.name for state in states if not state.ok]
        return JobResult(ok=not failing, checks=states, log_tail="\n".join(logs)[-20_000:],
                         error=f"failed: {', '.join(failing)}" if failing else "")  # fmt: skip
