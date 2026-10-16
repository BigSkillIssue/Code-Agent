"""Windows builds (W38a): the newest approved commit of a product, packed by its sandbox, tested
and packaged as MSIX by one of the admin's Windows machines (`forge-win-worker`), and the
package kept here for the Microsoft Store (W38b).

A build is also the worker's job, kept in the database so a restart loses none: "packing", then
"queued" until a worker asks for work, "running" until it reports, then "built" or "failed". A
job its worker never reports in time fails. Every build has the project's next build number, so
the package's version (1.0.<n>.0) is higher than the last one the Store saw.
"""

import asyncio
import contextlib
import hashlib
import logging
import secrets
import tarfile
import time
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from forge_web.db.engine import Database
from forge_web.db.store_models import WindowsBuild
from forge_web.hosting.plan import PlanProblem, SandboxCall, pack_commit
from forge_web.settings import WebSettings
from forge_winworker.wire import JobOffer, JobResult, MsixParams

log = logging.getLogger(__name__)
APP = "windows/App/App.csproj"  # `forge app new --windows` made the app
ACTIVE = ("packing", "queued", "running")


class JobRefused(Exception):
    """A worker asked for something that is not its running job."""


def view(row: WindowsBuild) -> dict[str, Any]:
    """A build as the pages show it."""
    return {"id": row.id, "commit": row.commit, "build_number": row.build_number,
            "version": row.version, "platform": row.platform, "status": row.status,
            "error": row.error, "hint": row.hint, "log_tail": row.log_tail,
            "tests_passed": row.tests_passed, "tests_failed": row.tests_failed,
            "file_name": row.file_name, "size": row.size, "created_at": row.created_at,
            "updated_at": row.updated_at}  # fmt: skip


def has_windows_app(archive: Path) -> bool:
    """Whether the packed commit has a Windows app."""
    with tarfile.open(archive, "r:gz") as tar:
        return any(name.removeprefix("./") == APP for name in tar.getnames())


class WindowsBuilds:
    """The Windows builds: packing them, handing them to workers, keeping what came back."""

    def __init__(
        self,
        db: Database,
        settings: WebSettings,
        call: SandboxCall,
        packing: dict[str, asyncio.Lock],
    ) -> None:
        self.db, self.settings, self.call = db, settings, call
        self.packing = packing  # shared with the deploys: a sandbox packs into one file
        self.root = settings.data_dir / "stores" / "windows"
        self.changed = asyncio.Condition()
        self.tasks: dict[str, asyncio.Task[None]] = {}

    def source(self, row: WindowsBuild) -> Path:
        return self.root / row.project_id / f"{row.id}.tar.gz"

    def package(self, row: WindowsBuild) -> Path:
        """Where a build's package is kept."""
        return self.root / row.project_id / f"{row.id}.msixupload"

    async def start(self) -> None:
        """Go on packing the builds a restart interrupted."""
        async with self.db.session() as session:
            ids = list(await session.scalars(
                select(WindowsBuild.id).where(WindowsBuild.status == "packing")))  # fmt: skip
        for build_id in ids:
            self.launch(build_id)

    async def close(self) -> None:
        tasks = list(self.tasks.values())
        for task in tasks:
            task.cancel()
        for task in tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task

    def launch(self, build_id: str) -> None:
        """Pack a build in the background; it waits for a worker then."""
        if build_id in self.tasks:
            return
        task = asyncio.create_task(self.pack(build_id))
        self.tasks[build_id] = task
        task.add_done_callback(lambda _: self.tasks.pop(build_id, None))

    async def notify(self) -> None:
        async with self.changed:
            self.changed.notify_all()

    async def add(self, project_id: str, user_id: str, commit: str) -> WindowsBuild:
        """A new build of this commit with the project's next build number (not packed yet)."""
        now = time.time()
        try:
            async with self.db.session() as session, session.begin():
                last = await session.scalar(select(func.max(WindowsBuild.build_number)).where(
                    WindowsBuild.project_id == project_id))  # fmt: skip
                number = (last or 0) + 1
                row = WindowsBuild(id=secrets.token_hex(16), project_id=project_id,
                                   user_id=user_id, commit=commit, build_number=number,
                                   version=f"1.0.{number}.0", created_at=now,
                                   updated_at=now)  # fmt: skip
                session.add(row)
        except IntegrityError:  # another build took this number a moment ago
            raise JobRefused("another Windows build of this app started just now") from None
        return row

    async def pack(self, build_id: str) -> None:
        """The commit packed by the project's sandbox; then the build waits for a worker."""
        async with self.db.session() as session:
            row = await session.get(WindowsBuild, build_id)
        if row is None:
            return
        target = self.source(row)
        try:
            max_bytes = self.settings.hosting.max_source_mb * 1024 * 1024
            async with self.packing.setdefault(row.project_id, asyncio.Lock()):
                await pack_commit(self.call, row.project_id, row.commit, target, max_bytes)
            if not await asyncio.to_thread(has_windows_app, target):
                target.unlink(missing_ok=True)
                await self.ended(build_id, JobResult(
                    ok=False, error="the product has no Windows app (windows/App)",
                    hint="make one with `forge app new --windows`"))  # fmt: skip
                return
        except PlanProblem as err:
            await self.ended(build_id, JobResult(ok=False, error=str(err), hint=err.hint))
            return
        await self.update(build_id, status="queued")
        await self.notify()

    async def update(self, build_id: str, **values: Any) -> None:
        async with self.db.session() as session, session.begin():
            row = await session.get(WindowsBuild, build_id)
            if row is not None:
                for name, value in values.items():
                    setattr(row, name, value)
                row.updated_at = time.time()

    async def claim(self, worker_id: str, wait_s: float) -> JobOffer | None:
        """The oldest waiting build as a job for this worker; waits up to `wait_s`."""
        deadline = time.monotonic() + wait_s
        while True:
            found = await self.take(worker_id)
            left = deadline - time.monotonic()
            if found is not None or left <= 0:
                return found
            async with self.changed:
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(self.changed.wait(), left)

    async def take(self, worker_id: str) -> JobOffer | None:
        async with self.db.session() as session, session.begin():
            row = await session.scalar(
                select(WindowsBuild).where(WindowsBuild.status == "queued")
                .order_by(WindowsBuild.created_at).limit(1)
            )  # fmt: skip
            if row is None:
                return None
            row.status, row.worker_id = "running", worker_id
            row.claimed_at = row.updated_at = time.time()
        project = hashlib.sha256(row.project_id.encode()).hexdigest()[:32]
        params = MsixParams.model_validate({"version": row.version, "platform": row.platform})
        return JobOffer(id=row.id, kind="msix", project=project, msix=params,
                        timeout_s=self.settings.stores.windows_timeout_s)  # fmt: skip

    async def running(self, worker_id: str, job_id: str) -> WindowsBuild:
        """The worker's running job with this id."""
        async with self.db.session() as session:
            row = await session.get(WindowsBuild, job_id)
        if row is None or row.worker_id != worker_id or row.status != "running":
            raise JobRefused("this is not one of this worker's running jobs")
        return row

    async def store_package(
        self, worker_id: str, job_id: str, parts: AsyncIterator[bytes], limit: int
    ) -> int:
        """Keep a running job's package (it arrives before the job's report); its bytes."""
        target = self.package(await self.running(worker_id, job_id))
        partial = target.with_suffix(".part")
        size = 0
        with partial.open("wb") as out:
            async for part in parts:
                size += len(part)
                if size > limit:
                    out.close()
                    partial.unlink(missing_ok=True)
                    raise JobRefused("the package is larger than this server keeps")
                await asyncio.to_thread(out.write, part)
        partial.replace(target)
        return size

    async def finish(self, worker_id: str, job_id: str, result: JobResult) -> None:
        """Keep the worker's report; a package counts only with the worker's success."""
        await self.running(worker_id, job_id)
        await self.ended(job_id, result)

    async def ended(self, build_id: str, result: JobResult) -> None:
        async with self.db.session() as session, session.begin():
            row = await session.get(WindowsBuild, build_id)
            if row is None:
                return
            package = self.package(row)
            made = result.ok and package.is_file()
            row.status = "built" if made else "failed"
            row.error = "" if made else (result.error or "the worker sent no package")
            row.hint, row.log_tail = result.hint, result.log_tail
            row.tests_passed, row.tests_failed = result.tests_passed, result.tests_failed
            row.file_name, row.size = (
                (result.file_name, package.stat().st_size) if made else ("", 0)
            )
            row.updated_at = time.time()
        await asyncio.to_thread(self.source(row).unlink, True)
        if not made:
            await asyncio.to_thread(package.unlink, True)

    async def expire(self) -> None:
        """Fail the jobs their workers did not report in time."""
        stores = self.settings.stores
        before = time.time() - stores.windows_timeout_s - stores.windows_grace_s
        async with self.db.session() as session:
            late = list(await session.scalars(select(WindowsBuild.id).where(
                WindowsBuild.status == "running", WindowsBuild.claimed_at < before)))  # fmt: skip
        for build_id in late:
            await self.ended(build_id, JobResult(
                ok=False, error="the Windows worker did not report the job in time",
                hint="is forge-win-worker still running? Start the build again"))  # fmt: skip
