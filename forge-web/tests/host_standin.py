"""A host for Forge Web's deploys (W26): the real `forge-host-worker` client, so polling, the
packed source and the sealed secrets go over HTTP as they would, with a runner that only
pretends to build and run and remembers what it was given."""

import asyncio
import contextlib
import os
import tarfile
import tempfile
from collections.abc import AsyncIterator
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey

from forge_hostworker.backup import outgoing
from forge_hostworker.client import HostClient
from forge_hostworker.wire import CheckState, HostJob, JobResult, ServiceState, public_key


class HostStandIn:
    """Answers every job as a healthy host would, unless told otherwise."""

    def __init__(self) -> None:
        self.key = X25519PrivateKey.generate()
        self.jobs: list[HostJob] = []
        self.sources: dict[str, set[str]] = {}  # job id -> the files of its packed source
        self.secrets: dict[str, dict[str, str]] = {}  # job id -> the secrets it opened
        self.archives: dict[str, bytes] = {}  # archive job id -> the backup it uploaded
        self.drilled: dict[str, bytes] = {}  # drill job id -> the backup it was given
        self.results: dict[str, JobResult] = {}  # kind -> the answer instead of success
        self.holds: dict[str, asyncio.Event] = {}  # kind -> answered once this is set
        self.data_dir = Path(tempfile.mkdtemp(prefix="host-standin-"))  # where uploads wait

    @property
    def public_key(self) -> str:
        return public_key(self.key)

    def kinds(self, environment: str | None = None) -> list[str]:
        """The kinds of the jobs it got, in order (of one environment, if given)."""
        return [j.kind for j in self.jobs if environment in (None, j.environment)]

    def last(self, kind: str) -> HostJob:
        return [job for job in self.jobs if job.kind == kind][-1]

    async def run(self, job: HostJob, source: Path | None, values: dict[str, str]) -> JobResult:
        self.jobs.append(job)
        if source is not None and job.kind != "restore_drill":
            with tarfile.open(source, "r:gz") as tar:
                self.sources[job.id] = {m.name for m in tar.getmembers() if m.isfile()}
        if job.kind in ("release", "archive", "restore_drill"):
            self.secrets[job.id] = values
        if source is not None and job.kind == "restore_drill":
            self.drilled[job.id] = source.read_bytes()
        if job.kind in self.holds:
            await self.holds[job.kind].wait()
        if job.kind == "archive" and job.kind not in self.results:
            sealed = outgoing(self.data_dir, job.id)
            sealed.parent.mkdir(parents=True, exist_ok=True)
            sealed.write_bytes(b"FBK1 encrypted " + job.id.encode() + os.urandom(64))
            self.archives[job.id] = sealed.read_bytes()
        return self.results.get(job.kind) or healthy(job)


def healthy(job: HostJob) -> JobResult:
    """What a host reports when everything works."""
    plan = job.plan
    if job.kind == "check" and plan is not None:
        checks = [CheckState(name=f"{s.name}: tests", ok=True) for s in plan.services if s.test]
        checks += [CheckState(name=f"{s.name}: migrations", ok=True)
                   for s in plan.services if s.migrate]  # fmt: skip
        return JobResult(ok=True, checks=checks)
    if job.kind == "restore_drill":
        restored = CheckState(name="restore", ok=True, detail="3 tables, 1 entries of files")
        return JobResult(ok=True, checks=[restored])
    if job.kind == "release" and plan is not None:
        services = [
            ServiceState(name=s.name, healthy=True,
                         container=f"forge-{plan.app}-{plan.environment}-{s.name}-r{plan.release}")
            for s in plan.services
        ]  # fmt: skip
        return JobResult(ok=True, release=plan.release, services=services)
    return JobResult(ok=True)


@contextlib.asynccontextmanager
async def host_online(url: str, token: str, host: HostStandIn) -> AsyncIterator[None]:
    """The stand-in connected to the server while the block runs."""
    client = HostClient(url, token, host, host.key, slots=2)  # type: ignore[arg-type]
    stop = asyncio.Event()
    serving = asyncio.create_task(client.serve(stop))
    try:
        yield
    finally:
        for hold in host.holds.values():
            hold.set()
        stop.set()
        with contextlib.suppress(asyncio.CancelledError, TimeoutError):
            await asyncio.wait_for(serving, 10)
