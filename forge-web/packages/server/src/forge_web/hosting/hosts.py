"""Hosts and their jobs: tokens (shown once, stored as a hash), how a request proves to be a
host, and the queue of jobs, kept in the database so a restart of the server loses none.

A job waits as "queued" until its host asks for work, then is "running" until the host reports
("done"). A host gets one job per app at a time, so the steps of a deploy keep their order. A job
the host never reports in time is "lost". Each job's packed source lives in
`<data>/hosting/jobs/<id>/` until the job ends; its sealed secrets are handed out once.
"""

import asyncio
import base64
import binascii
import contextlib
import hashlib
import hmac
import secrets
import shutil
import time
from pathlib import Path

from sqlalchemy import select

from forge_hostworker.wire import TOKEN_PREFIX, HostJob, JobResult, SealedSecrets
from forge_web.db.engine import Database
from forge_web.db.hosting_models import HostedApp, HostJobRow, HostServer

SEEN_EVERY_S = 30  # last_seen is written at most this often
ONLINE_S = 120  # a host seen this recently counts as connected
SOURCE = "source.tar.gz"
RECHECK_S = 5.0  # waiting for a result also looks at the database this often


class JobGone(Exception):
    """A host asked for something that is not its running job."""


def secret_hash(secret: str) -> str:
    """What is stored instead of the token's secret part."""
    return hashlib.sha256(secret.encode()).hexdigest()


def valid_public_key(value: str) -> bool:
    """Whether a host's public key is 32 bytes of base64 (X25519)."""
    try:
        return len(base64.b64decode(value, validate=True)) == 32
    except (binascii.Error, ValueError):
        return False


async def create_host(db: Database, name: str, public_key: str) -> tuple[HostServer, str]:
    """A new host and its token (`fhw_<id>_<secret>`); the token is not stored."""
    if not valid_public_key(public_key):
        raise ValueError("the public key is not one `forge-host-worker keygen` printed")
    host_id, secret = secrets.token_hex(8), secrets.token_urlsafe(32)
    row = HostServer(id=host_id, name=name, token_hash=secret_hash(secret), public_key=public_key,
                     enabled=True, created_at=time.time())  # fmt: skip
    async with db.session() as session, session.begin():
        session.add(row)
    return row, f"{TOKEN_PREFIX}_{host_id}_{secret}"


async def host_of(db: Database, authorization: str, version: str = "") -> HostServer | None:
    """The enabled host whose token the header carries, else None (and mark it seen)."""
    if not authorization.lower().startswith("bearer "):
        return None
    parts = authorization[7:].strip().split("_", 2)
    if len(parts) != 3 or parts[0] != TOKEN_PREFIX:
        return None
    async with db.session() as session, session.begin():
        row = await session.get(HostServer, parts[1])
        if row is None or not hmac.compare_digest(row.token_hash, secret_hash(parts[2])):
            return None
        if not row.enabled:
            return None
        now = time.time()
        if now - row.last_seen > SEEN_EVERY_S or (version and version != row.version):
            row.last_seen = now
            row.version = version or row.version
    return row


async def pick_host(db: Database) -> HostServer | None:
    """The host a new app goes to: the enabled one with the fewest apps (None: no host)."""
    async with db.session() as session:
        hosts = list(await session.scalars(select(HostServer).where(HostServer.enabled)))
        apps = list(await session.scalars(select(HostedApp.host_id)))
    if not hosts:
        return None
    return min(hosts, key=lambda host: (apps.count(host.id), host.created_at))


class HostQueue:
    """The hosts' jobs, and the deploys waiting for their results."""

    def __init__(self, db: Database, data_dir: Path, grace_s: float) -> None:
        self.db = db
        self.root = data_dir / "hosting" / "jobs"
        self.grace_s = grace_s
        self.changed = asyncio.Condition()

    async def notify(self) -> None:
        """Wake whoever waits for a job or a result."""
        async with self.changed:
            self.changed.notify_all()

    async def send(
        self,
        host_id: str,
        deploy_id: str,
        job: HostJob,
        source: Path | None = None,
        sealed: SealedSecrets | None = None,
    ) -> None:
        """Queue a job for a host (its source copied, its secrets sealed already)."""
        if source is not None:
            folder = self.root / job.id
            folder.mkdir(parents=True, exist_ok=True)
            await asyncio.to_thread(shutil.copyfile, source, folder / SOURCE)
        row = HostJobRow(id=job.id, host_id=host_id, deploy_id=deploy_id, app=job.app,
                         kind=job.kind, job=job.model_dump_json(), status="queued",
                         sealed=sealed.model_dump_json() if sealed else "",
                         created_at=time.time())  # fmt: skip
        async with self.db.session() as session, session.begin():
            session.add(row)
        await self.notify()

    async def exists(self, job_id: str) -> bool:
        """Whether a job with this id was queued."""
        async with self.db.session() as session:
            return await session.get(HostJobRow, job_id) is not None

    async def claim(self, host_id: str, wait_s: float) -> HostJob | None:
        """The host's oldest queued job of an app with nothing running; waits up to `wait_s`."""
        deadline = time.monotonic() + wait_s
        while True:
            found = await self._take(host_id)
            left = deadline - time.monotonic()
            if found is not None or left <= 0:
                return found
            async with self.changed:
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(self.changed.wait(), left)

    async def _take(self, host_id: str) -> HostJob | None:
        async with self.db.session() as session, session.begin():
            rows = list(await session.scalars(
                select(HostJobRow).where(HostJobRow.host_id == host_id,
                                         HostJobRow.status.in_(("queued", "running")))
                .order_by(HostJobRow.created_at)
            ))  # fmt: skip
            busy = {row.app for row in rows if row.status == "running"}
            for row in rows:
                if row.status == "queued" and row.app not in busy:
                    row.status, row.claimed_at = "running", time.time()
                    return HostJob.model_validate_json(row.job)
        return None

    async def running(self, host_id: str, job_id: str) -> HostJobRow:
        """The host's running job with this id."""
        async with self.db.session() as session:
            row = await session.get(HostJobRow, job_id)
        if row is None or row.host_id != host_id or row.status != "running":
            raise JobGone("this is not one of this host's running jobs")
        return row

    async def source(self, host_id: str, job_id: str) -> Path:
        """The packed source of the host's running job."""
        await self.running(host_id, job_id)
        path = self.root / job_id / SOURCE
        if not path.is_file():
            raise JobGone("this job has no source")
        return path

    async def secrets_of(self, host_id: str, job_id: str) -> SealedSecrets:
        """The job's sealed secrets, handed out once."""
        async with self.db.session() as session, session.begin():
            row = await session.get(HostJobRow, job_id)
            if row is None or row.host_id != host_id or row.status != "running" or not row.sealed:
                raise JobGone("this job has no secrets (any more)")
            sealed, row.sealed = row.sealed, ""
        return SealedSecrets.model_validate_json(sealed)

    async def finish(self, host_id: str, job_id: str, result: JobResult) -> None:
        """Keep the host's report and wake the deploy that waits for it."""
        async with self.db.session() as session, session.begin():
            row = await session.get(HostJobRow, job_id)
            if row is None or row.host_id != host_id or row.status != "running":
                raise JobGone("this is not one of this host's running jobs")
            row.status, row.result, row.sealed = "done", result.model_dump_json(), ""
            row.finished_at = time.time()
        await asyncio.to_thread(shutil.rmtree, self.root / job_id, True)
        await self.notify()

    async def result(self, job_id: str) -> JobResult:
        """Wait for the job's result; a job its host never takes or never reports is lost."""
        while True:
            outcome = await self._outcome(job_id)
            if outcome is not None:
                return outcome
            async with self.changed:
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(self.changed.wait(), RECHECK_S)

    async def _outcome(self, job_id: str) -> JobResult | None:
        async with self.db.session() as session, session.begin():
            row = await session.get(HostJobRow, job_id)
            if row is None:
                return JobResult(ok=False, error="the job is gone")
            if row.status in ("done", "lost"):
                return JobResult.model_validate_json(row.result)
            lost = self._lost(row)
            if not lost:
                return None
            row.status, row.sealed, row.finished_at = "lost", "", time.time()
            row.result = JobResult(ok=False, error=lost).model_dump_json()
        await asyncio.to_thread(shutil.rmtree, self.root / job_id, True)
        return JobResult(ok=False, error=lost)

    def _lost(self, row: HostJobRow) -> str:
        """Why a job counts as lost ("" while it may still come)."""
        now = time.time()
        if row.status == "queued" and now > row.created_at + self.grace_s:
            return "no host took the job; is forge-host-worker running?"
        timeout = HostJob.model_validate_json(row.job).timeout_s
        if row.status == "running" and now > row.claimed_at + timeout + self.grace_s:
            return "the host did not report the job in time"
        return ""
