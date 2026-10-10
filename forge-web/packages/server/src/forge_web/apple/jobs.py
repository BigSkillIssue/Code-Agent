"""Apple jobs: a sandbox's request waits here until a Mac takes it and reports back.

Each job keeps its files in `<data>/apple/<job id>/`: the packed project (deleted once the job
ends) and, for archives and exports, what the Mac sent (the `.xcarchive`, or the signed `.ipa` or
`.pkg`, kept for the App Store step). The server never unpacks either. The signing material of
an export job lives only in memory and is handed out once.
"""

import asyncio
import contextlib
import hashlib
import json
import shutil
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import func, select, update

from forge_macworker.wire import (
    BuildParams,
    ExportParams,
    JobOffer,
    JobResult,
    ScreenshotParams,
    SigningMaterial,
    knows_exports,
    knows_store_shots,
)
from forge_web.db.engine import Database
from forge_web.db.models import AppleJob
from forge_web.gateway.meter import month_start
from forge_web.settings import AppleSettings

SOURCE, ARCHIVE = "source.tar.gz", "archive.tar.gz"
KINDS: dict[type, str] = {
    BuildParams: "build",
    ScreenshotParams: "screenshot",
    ExportParams: "export",
}


class JobRefused(Exception):
    """A worker asked for something that is not its job, or not in this state."""


@dataclass
class NewJob:
    """What a sandbox asked for."""

    project_id: str
    chat_id: str
    user_id: str
    params: BuildParams | ScreenshotParams | ExportParams
    signing: SigningMaterial | None = None  # export jobs only


class AppleJobs:
    """The queue of Apple jobs, and the sandboxes waiting for their results."""

    def __init__(self, db: Database, settings: AppleSettings, data_dir: Path) -> None:
        self.db = db
        self.settings = settings
        self.root = data_dir / "apple"
        self.waiting: dict[str, asyncio.Future[JobResult]] = {}
        self.signing: dict[str, SigningMaterial] = {}  # export jobs' material, until fetched
        self.queued = asyncio.Condition()

    async def start(self) -> None:
        """Jobs from before a restart have nobody waiting for them: they failed."""
        async with self.db.session() as session, session.begin():
            await session.execute(
                update(AppleJob).where(AppleJob.status.in_(("queued", "running")))
                .values(status="failed", outcome="the server restarted", finished_at=time.time())
            )  # fmt: skip

    async def run(self, job: NewJob, source: Path) -> JobResult:
        """Queue a job with its packed project and wait for the result (or the time limit)."""
        job_id = await self._queue(job, source)
        future: asyncio.Future[JobResult] = asyncio.get_running_loop().create_future()
        self.waiting[job_id] = future
        async with self.queued:
            self.queued.notify_all()
        try:
            return await asyncio.wait_for(future, self.settings.job_timeout_s)
        except TimeoutError:
            await self._end(job_id, "failed", "no Mac finished it in time", 0.0)
            return JobResult(ok=False, error="no Mac finished the job in time",
                             hint="the Macs may be busy or offline; try again later")  # fmt: skip
        finally:
            self.waiting.pop(job_id, None)
            self.signing.pop(job_id, None)
            with contextlib.suppress(FileNotFoundError):
                (self.root / job_id / SOURCE).unlink()

    async def _queue(self, job: NewJob, source: Path) -> str:
        job_id = hashlib.sha256(f"{source}{time.time_ns()}".encode()).hexdigest()[:32]
        folder = self.root / job_id
        folder.mkdir(parents=True, exist_ok=True)
        shutil.move(str(source), folder / SOURCE)
        kind = KINDS[type(job.params)]
        if job.signing is not None:
            self.signing[job_id] = job.signing
        row = AppleJob(id=job_id, project_id=job.project_id, chat_id=job.chat_id,
                       user_id=job.user_id, kind=kind, params=job.params.model_dump_json(),
                       status="queued", created_at=time.time())  # fmt: skip
        async with self.db.session() as session, session.begin():
            session.add(row)
        return job_id

    async def claim(self, worker_id: str, wait_s: float, version: str = "") -> JobOffer | None:
        """The oldest waiting job for this worker; waits up to `wait_s` for one to come. Jobs a
        worker's version does not know (exports, store screenshots) go to newer workers only."""
        deadline = time.monotonic() + wait_s
        async with self.queued:
            while (offer := await self._take(worker_id, version)) is None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(self.queued.wait(), remaining)
            return offer

    async def _take(self, worker_id: str, version: str) -> JobOffer | None:
        async with self.db.session() as session, session.begin():
            rows = await session.scalars(
                select(AppleJob).where(AppleJob.status == "queued").order_by(AppleJob.created_at)
            )
            fits = (r for r in rows if r.id in self.waiting and can_run(r, version))
            row = next(fits, None)
            if row is None:
                return None
            row.status, row.worker_id, row.started_at = "running", worker_id, time.time()
        return offer_of(row, self.settings.job_timeout_s)

    async def running(self, worker_id: str, job_id: str) -> AppleJob:
        """The job, if this worker runs it now."""
        async with self.db.session() as session:
            row = await session.get(AppleJob, job_id)
        if row is None or row.worker_id != worker_id or row.status != "running":
            raise JobRefused("not a running job of this worker")
        return row

    def source(self, job_id: str) -> Path:
        """The packed project of a job (for an export: the archive to sign)."""
        return self.root / job_id / SOURCE

    async def signing_of(self, worker_id: str, job_id: str) -> SigningMaterial:
        """An export job's signing material, once, to the worker that runs the job."""
        row = await self.running(worker_id, job_id)
        material = self.signing.pop(job_id, None) if row.kind == "export" else None
        if material is None:
            raise JobRefused("no signing material for this job (it is handed out only once)")
        return material

    async def store_archive(
        self, worker_id: str, job_id: str, chunks: AsyncIterator[bytes]
    ) -> None:
        """Keep the archive a worker sends for its running archive job (size-limited)."""
        row = await self.running(worker_id, job_id)
        params = BuildParams.model_validate_json(row.params) if row.kind == "build" else None
        if row.kind != "export" and (params is None or params.action != "archive"):
            raise JobRefused("this job makes no archive")
        limit, size = self.settings.max_archive_mb * 1024 * 1024, 0
        target = self.root / job_id / ARCHIVE
        with target.open("wb") as out:
            async for chunk in chunks:
                size += len(chunk)
                if size > limit:
                    out.close()
                    target.unlink()
                    raise JobRefused("the archive is larger than the server takes")
                out.write(chunk)

    async def finish(self, worker_id: str, job_id: str, result: JobResult) -> None:
        """A worker's report: record it and hand it to the waiting sandbox."""
        row = await self.running(worker_id, job_id)
        archive = (self.root / job_id / ARCHIVE).is_file()
        if result.build is not None:
            # The Mac's paths mean nothing here; an archive is known by its job.
            result.build.artifact = f"job:{job_id}" if archive and result.build.ok else ""
        if result.export is not None:
            result.export.artifact = f"job:{job_id}" if archive and result.export.ok else ""
        seconds = max(0.0, time.time() - row.started_at)
        status = "done" if result.ok else "failed"
        await self._end(job_id, status, outcome(result), seconds, archive=archive)
        future = self.waiting.get(job_id)
        if future is not None and not future.done():
            future.set_result(result)

    async def _end(
        self, job_id: str, status: str, summary: str, seconds: float, archive: bool = False
    ) -> None:
        async with self.db.session() as session, session.begin():
            row = await session.get(AppleJob, job_id)
            if row is not None and row.status in ("queued", "running"):
                row.status, row.outcome, row.seconds = status, summary[:500], seconds
                row.finished_at, row.archive = time.time(), archive

    async def minutes_used(self, user_id: str) -> float:
        """Mac minutes a user's jobs took this month."""
        async with self.db.session() as session:
            total = await session.scalar(
                select(func.coalesce(func.sum(AppleJob.seconds), 0.0)).where(
                    AppleJob.user_id == user_id, AppleJob.created_at >= month_start()
                )
            )
        return float(total or 0.0) / 60


def can_run(row: AppleJob, version: str) -> bool:
    """Whether a worker of this version knows this kind of job."""
    if row.kind == "export":
        return knows_exports(version)
    if row.kind == "screenshot" and json.loads(row.params).get("fit"):
        return knows_store_shots(version)
    return True


def offer_of(row: AppleJob, timeout_s: float) -> JobOffer:
    """The job as a worker sees it: its project only as a key."""
    project = hashlib.sha256(f"forge-web-project:{row.project_id}".encode()).hexdigest()[:32]
    params = json.loads(row.params)
    if row.kind == "export":
        return JobOffer(id=row.id, kind="export", project=project, timeout_s=timeout_s,
                        export=ExportParams.model_validate(params))  # fmt: skip
    if row.kind == "build":
        return JobOffer(id=row.id, kind="build", project=project, timeout_s=timeout_s,
                        build=BuildParams.model_validate(params))  # fmt: skip
    return JobOffer(id=row.id, kind="screenshot", project=project, timeout_s=timeout_s,
                    screenshot=ScreenshotParams.model_validate(params))  # fmt: skip


def outcome(result: JobResult) -> str:
    """One line for the admin page."""
    if not result.ok:
        return f"could not run: {result.error}"
    if result.build is not None:
        verdict = "succeeded" if result.build.ok else "failed"
        return f"{result.build.action} for {result.build.platform}: {verdict}"
    if result.screen is not None:
        return f"screenshot of {result.screen.device}"
    if result.export is not None:
        verdict = "succeeded" if result.export.ok else "failed"
        return f"export for {result.export.platform}: {verdict}"
    return "done"
