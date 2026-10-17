"""Windows builds (W38a, W39): the newest approved commit of a product, packed by its sandbox,
tested and published by one of the admin's Windows build machines (`forge-win-worker`), packed
by Velopack into an installer, a ZIP and update files, and kept here for the product's website.

When the admin has a signing machine, the build machine sends the published app and the signing
machine packs and signs it: it never runs a product's code, so its key stays safe. Without one
the build machine packs the app unsigned itself.

A build is also the workers' jobs, kept in the database so a restart loses none: "packing",
then "queued" until a build machine asks for work, "running" until it reports; a signed build
then is "pack_queued" until a signing machine asks, "pack_running" until it reports; then
"built" or "failed". A job its worker never reports in time fails. Every build has the
project's next build number, so its version (1.0.<n>) is higher than the last one.
"""

import asyncio
import contextlib
import hashlib
import json
import logging
import re
import secrets
import shutil
import tarfile
import time
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from forge_web.db.engine import Database
from forge_web.db.models import Project
from forge_web.db.store_models import WindowsBuild, WinWorker
from forge_web.hosting.plan import PlanProblem, SandboxCall, pack_commit
from forge_web.settings import WebSettings
from forge_web.stores.windows_files import ReleaseProblem, check_app, unpack_release
from forge_winworker.wire import BuildParams, JobOffer, JobResult, PackParams

log = logging.getLogger(__name__)
APP = "windows/App/App.csproj"  # `forge app new --windows` made the app
ACTIVE = ("packing", "queued", "running", "pack_queued", "pack_running")
RUNNING = ("running", "pack_running")


class JobRefused(Exception):
    """A worker asked for something that is not its running job."""


def view(row: WindowsBuild) -> dict[str, Any]:
    """A build as the pages show it."""
    return {"id": row.id, "commit": row.commit, "build_number": row.build_number,
            "version": row.version, "platform": row.platform, "status": row.status,
            "error": row.error, "hint": row.hint, "log_tail": row.log_tail,
            "tests_passed": row.tests_passed, "tests_failed": row.tests_failed,
            "file_name": row.file_name, "size": row.size, "signed": row.signed,
            "files": json.loads(row.files) if row.files else {},
            "created_at": row.created_at, "updated_at": row.updated_at}  # fmt: skip


def has_windows_app(archive: Path) -> bool:
    """Whether the packed commit has a Windows app."""
    with tarfile.open(archive, "r:gz") as tar:
        return any(name.removeprefix("./") == APP for name in tar.getnames())


def title_of(name: str) -> str:
    """A project's name as the installer and the Start menu show it."""
    shown = re.sub(r"[\x00-\x1f\"]+", " ", name).strip()[:100]
    return shown or "App"


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

    def app_zip(self, row: WindowsBuild) -> Path:
        """The published app a build machine sent for the signing machine."""
        return self.root / row.project_id / f"{row.id}.app.zip"

    def upload(self, row: WindowsBuild) -> Path:
        """What the running job sends."""
        return self.root / row.project_id / f"{row.id}.upload"

    def release(self, row: WindowsBuild) -> Path:
        """Where a build's release files are kept."""
        return self.root / row.project_id / row.id

    def job_input(self, row: WindowsBuild) -> Path:
        """What the running job downloads."""
        return self.app_zip(row) if row.status == "pack_running" else self.source(row)

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
        """A new build of this commit with the project's next build number (not packed yet);
        signed when a signing machine is there to sign it."""
        now = time.time()
        try:
            async with self.db.session() as session, session.begin():
                last = await session.scalar(select(func.max(WindowsBuild.build_number)).where(
                    WindowsBuild.project_id == project_id))  # fmt: skip
                signer = await session.scalar(select(WinWorker.id).where(
                    WinWorker.signs.is_(True), WinWorker.enabled.is_(True)).limit(1))  # fmt: skip
                number = (last or 0) + 1
                row = WindowsBuild(id=secrets.token_hex(16), project_id=project_id,
                                   user_id=user_id, commit=commit, build_number=number,
                                   version=f"1.0.{number}", signed=signer is not None,
                                   created_at=now, updated_at=now)  # fmt: skip
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

    async def claim(self, worker: WinWorker, wait_s: float) -> JobOffer | None:
        """The oldest job for this worker; waits up to `wait_s`. A signing machine gets only
        pack jobs, a build machine only build jobs."""
        deadline = time.monotonic() + wait_s
        while True:
            found = await self.take(worker)
            left = deadline - time.monotonic()
            if found is not None or left <= 0:
                return found
            async with self.changed:
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(self.changed.wait(), left)

    async def take(self, worker: WinWorker) -> JobOffer | None:
        waiting, running = (
            ("pack_queued", "pack_running") if worker.signs else ("queued", "running")
        )
        async with self.db.session() as session, session.begin():
            found = (await session.execute(
                select(WindowsBuild, Project.name)
                .join(Project, Project.id == WindowsBuild.project_id)
                .where(WindowsBuild.status == waiting).order_by(WindowsBuild.created_at).limit(1)
            )).first()  # fmt: skip
            if found is None:
                return None
            row, name = found
            row.status, row.worker_id = running, worker.id
            row.claimed_at = row.updated_at = time.time()
        project = hashlib.sha256(row.project_id.encode()).hexdigest()[:32]
        timeout = self.settings.stores.windows_timeout_s
        values = {"version": row.version, "platform": row.platform, "title": title_of(name)}
        if worker.signs:
            pack = PackParams.model_validate(values | {"pack_id": row.pack_id})
            return JobOffer(id=row.id, kind="pack", project=project, pack=pack, timeout_s=timeout)
        build = BuildParams.model_validate(values | {"pack": not row.signed})
        return JobOffer(id=row.id, kind="build", project=project, build=build, timeout_s=timeout)

    async def running(self, worker_id: str, job_id: str) -> WindowsBuild:
        """The worker's running job with this id."""
        async with self.db.session() as session:
            row = await session.get(WindowsBuild, job_id)
        if row is None or row.worker_id != worker_id or row.status not in RUNNING:
            raise JobRefused("this is not one of this worker's running jobs")
        return row

    async def store_package(
        self, worker_id: str, job_id: str, parts: AsyncIterator[bytes], limit: int
    ) -> int:
        """Keep what a running job sent (it arrives before the job's report); its bytes."""
        target = self.upload(await self.running(worker_id, job_id))
        partial = target.with_suffix(".part")
        size = 0
        with partial.open("wb") as out:
            async for part in parts:
                size += len(part)
                if size > limit:
                    out.close()
                    partial.unlink(missing_ok=True)
                    raise JobRefused("what the job sent is larger than this server keeps")
                await asyncio.to_thread(out.write, part)
        partial.replace(target)
        return size

    async def finish(self, worker_id: str, job_id: str, result: JobResult) -> None:
        """Keep the worker's report; what it sent counts only with its success. A signed
        build's published app goes on to a signing machine."""
        row = await self.running(worker_id, job_id)
        if row.status == "running" and row.signed and result.ok:
            await self.handed_on(row, result)
        else:
            await self.ended(job_id, result)

    async def handed_on(self, row: WindowsBuild, result: JobResult) -> None:
        """The published app checked and kept for a signing machine."""
        upload, target = self.upload(row), self.app_zip(row)
        try:
            if not result.pack_id or not upload.is_file():
                raise ReleaseProblem("the build machine sent no published app")
            await asyncio.to_thread(check_app, upload, result.pack_id)
        except ReleaseProblem as err:
            await self.ended(row.id, result.model_copy(update={"ok": False, "error": str(err)}))
            return
        upload.replace(target)
        await asyncio.to_thread(self.source(row).unlink, True)
        await self.update(row.id, status="pack_queued", worker_id="", pack_id=result.pack_id,
                          tests_passed=result.tests_passed, log_tail=result.log_tail)  # fmt: skip
        await self.notify()

    async def ended(self, build_id: str, result: JobResult) -> None:
        """The build's end: its release checked and kept, or the reason it failed."""
        async with self.db.session() as session:
            row = await session.get(WindowsBuild, build_id)
        if row is None:
            return
        files: dict[str, Any] = {}
        error = "" if result.ok else (result.error or "the job failed")
        if result.ok:
            try:
                files = await self.kept(row, result)
            except ReleaseProblem as err:
                error = str(err)
        async with self.db.session() as session, session.begin():
            row = await session.get(WindowsBuild, build_id)
            if row is None:
                return
            row.status, row.error = ("failed", error) if error else ("built", "")
            row.hint = result.hint if error else ""
            if result.log_tail or row.status == "failed":
                row.log_tail = result.log_tail or row.log_tail
            row.tests_passed = max(row.tests_passed, result.tests_passed)
            row.tests_failed = result.tests_failed
            row.pack_id = row.pack_id or result.pack_id
            row.files = json.dumps(files) if files else ""
            row.size = sum(int(f["size"]) for f in files.values())
            row.file_name = next((name for name in files if name.endswith("-Setup.exe")), "")
            row.updated_at = time.time()
        for leftover in (self.source(row), self.app_zip(row), self.upload(row)):
            await asyncio.to_thread(leftover.unlink, True)
        if error:
            await asyncio.to_thread(shutil.rmtree, self.release(row), True)

    async def kept(self, row: WindowsBuild, result: JobResult) -> dict[str, Any]:
        """The release a job sent, unpacked and checked."""
        pack_id = row.pack_id or result.pack_id
        if not pack_id or not self.upload(row).is_file():
            raise ReleaseProblem("the worker sent no release")
        if row.pack_id and result.pack_id != row.pack_id:
            raise ReleaseProblem("the signing machine packed another app")
        if row.signed and not result.signed:
            raise ReleaseProblem("the release was not signed (is --sign-template set?)")
        limit = self.settings.stores.max_package_mb * 1024 * 1024
        return await asyncio.to_thread(unpack_release, self.upload(row), self.release(row),
                                       pack_id, row.version, row.signed, limit)  # fmt: skip

    async def expire(self) -> None:
        """Fail the jobs their workers did not report in time."""
        stores = self.settings.stores
        before = time.time() - stores.windows_timeout_s - stores.windows_grace_s
        async with self.db.session() as session:
            late = list(await session.scalars(select(WindowsBuild.id).where(
                WindowsBuild.status.in_(RUNNING), WindowsBuild.claimed_at < before)))  # fmt: skip
        for build_id in late:
            await self.ended(build_id, JobResult(
                ok=False, error="the Windows worker did not report the job in time",
                hint="is forge-win-worker still running? Start the build again"))  # fmt: skip
