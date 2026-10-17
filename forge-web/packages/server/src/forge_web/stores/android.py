"""Android builds (W37a): the newest approved commit of a product built into an unsigned release
bundle on the app's own host (`forge-host-worker`: a throwaway container without secrets, on the
build network only), and the bundle kept on this server for Google Play (W37b). Every build has
its own version code, one higher than the project's last, as Google Play wants for each upload.

A build remembers its job's id before sending it, so after a restart it waits for that same job
instead of building twice.
"""

import asyncio
import contextlib
import logging
import re
import secrets
import tarfile
import time
import zipfile
from pathlib import Path
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from forge_hostworker.wire import AndroidBuild as BuildWith
from forge_hostworker.wire import HostJob, JobResult
from forge_web.db.engine import Database
from forge_web.db.hosting_models import HostedApp
from forge_web.db.store_models import AndroidBuild
from forge_web.hosting.hosts import HostQueue
from forge_web.hosting.plan import PlanProblem, SandboxCall, pack_commit
from forge_web.settings import WebSettings

log = logging.getLogger(__name__)
JOB_TIMEOUT_S = 4_500.0  # the host's hour of Gradle, with unpacking and keeping the bundle
GRADLEW = "android/gradlew"  # `forge app new --android` made the app
GRADLE_APP = "android/app/build.gradle.kts"  # its application ID, which Google Play knows it by
APPLICATION_ID = re.compile(r'^\s*applicationId\s*=\s*"([A-Za-z]\w*(?:\.[A-Za-z]\w*)+)"', re.M)


class BuildProblem(Exception):
    """A build cannot go on; with a hint for the user."""

    def __init__(self, message: str, hint: str = "") -> None:
        super().__init__(message)
        self.hint = hint


def view(row: AndroidBuild) -> dict[str, Any]:
    """A build as the pages show it."""
    return {"id": row.id, "commit": row.commit, "version_code": row.version_code,
            "version_name": row.version_name, "status": row.status, "error": row.error,
            "hint": row.hint, "log_tail": row.log_tail, "size": row.size,
            "package_name": row.package_name,
            "created_at": row.created_at, "updated_at": row.updated_at}  # fmt: skip


def android_app(archive: Path) -> str | None:
    """The application ID of the packed commit's Android app ("" if its Gradle file names
    none), or None when the commit has no Android app (no Gradle wrapper)."""
    with tarfile.open(archive, "r:gz") as tar:
        members = {member.name.removeprefix("./"): member for member in tar.getmembers()}
        if GRADLEW not in members:
            return None
        gradle = members.get(GRADLE_APP)
        handle = tar.extractfile(gradle) if gradle is not None and gradle.isfile() else None
        text = handle.read(1024 * 1024).decode("utf-8", "replace") if handle else ""
    found = APPLICATION_ID.search(text)
    return found.group(1) if found else ""


def is_bundle(path: Path) -> bool:
    """Whether a file is an Android App Bundle (a ZIP with the base module's manifest)."""
    if not path.is_file() or not zipfile.is_zipfile(path):
        return False
    try:
        with zipfile.ZipFile(path) as bundle:
            return "base/manifest/AndroidManifest.xml" in bundle.namelist()
    except (zipfile.BadZipFile, OSError):
        return False


class AndroidBuilds:
    """Runs the Android builds, one task each."""

    def __init__(
        self,
        db: Database,
        settings: WebSettings,
        queue: HostQueue,
        call: SandboxCall,
        packing: dict[str, asyncio.Lock],
    ) -> None:
        self.db, self.settings, self.queue, self.call = db, settings, queue, call
        self.packing = packing  # shared with the deploys: a sandbox packs into one file
        self.root = settings.data_dir / "stores" / "android"
        self.tasks: dict[str, asyncio.Task[None]] = {}

    async def start(self) -> None:
        """Go on with the builds a restart interrupted."""
        async with self.db.session() as session:
            ids = list(await session.scalars(
                select(AndroidBuild.id).where(AndroidBuild.status == "building")))  # fmt: skip
        for build_id in ids:
            self.launch(build_id)

    async def close(self) -> None:
        """Stop the tasks; their builds stay "building" and go on after the next start."""
        tasks = list(self.tasks.values())
        for task in tasks:
            task.cancel()
        for task in tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task

    def launch(self, build_id: str) -> None:
        """Run (or go on with) a build in the background."""
        if build_id in self.tasks:
            return
        task = asyncio.create_task(self.run(build_id))
        self.tasks[build_id] = task
        task.add_done_callback(lambda _: self.tasks.pop(build_id, None))

    def bundle(self, row: AndroidBuild) -> Path:
        """Where a build's bundle is kept."""
        return self.root / row.project_id / f"{row.id}.aab"

    async def add(self, project_id: str, user_id: str, commit: str) -> AndroidBuild:
        """A new build of this commit with the project's next version code (not started)."""
        now = time.time()
        try:
            async with self.db.session() as session, session.begin():
                last = await session.scalar(select(func.max(AndroidBuild.version_code)).where(
                    AndroidBuild.project_id == project_id))  # fmt: skip
                code = (last or 0) + 1
                row = AndroidBuild(id=secrets.token_hex(16), project_id=project_id,
                                   user_id=user_id, commit=commit, version_code=code,
                                   version_name=f"1.0.{code}", created_at=now,
                                   updated_at=now)  # fmt: skip
                session.add(row)
        except IntegrityError:  # another build took this version code a moment ago
            raise BuildProblem("another Android build of this app started just now") from None
        return row

    async def target_of(self, job_id: str) -> Path:
        """Where the bundle of a running build's job is kept."""
        async with self.db.session() as session:
            row = await session.scalar(select(AndroidBuild).where(
                AndroidBuild.job_id == job_id, AndroidBuild.status == "building"))  # fmt: skip
        if row is None:
            raise LookupError("no such android job")
        return self.bundle(row)

    async def run(self, build_id: str) -> None:
        """The build from packing to its bundle; whatever goes wrong ends it as failed."""
        try:
            await self.ended(build_id, await self.built(build_id))
        except (BuildProblem, PlanProblem) as err:
            await self.ended(build_id, JobResult(ok=False, error=str(err), hint=err.hint))
        except asyncio.CancelledError:
            raise
        except Exception as err:  # a bug: the build must still not stay "building" forever
            log.exception("android build %s failed", build_id)
            await self.ended(
                build_id, JobResult(ok=False, error=f"the build stopped: {err!r}"[:2000])
            )

    async def built(self, build_id: str) -> JobResult:
        """The host's report on the build's job (the same job again after a restart)."""
        async with self.db.session() as session:
            row = await session.get(AndroidBuild, build_id)
            hosted = await session.get(HostedApp, row.project_id) if row else None
        if row is None or hosted is None:
            raise BuildProblem("the app is no longer on a host")
        if row.job_id and await self.queue.exists(row.job_id):
            return await self.queue.result(row.job_id)
        source = self.root / row.project_id / f"{row.id}.tar.gz"
        try:
            max_bytes = self.settings.hosting.max_source_mb * 1024 * 1024
            async with self.packing.setdefault(row.project_id, asyncio.Lock()):
                await pack_commit(self.call, row.project_id, row.commit, source, max_bytes)
            package = await asyncio.to_thread(android_app, source)
            if package is None:
                raise BuildProblem("the product has no Android app (android/gradlew)",
                                   "make one with `forge app new --android`")  # fmt: skip
            job = self.job_for(row, hosted.app)
            await self.remember(build_id, job.id, package)
            await self.queue.send(hosted.host_id, "", job, source)
        finally:
            source.unlink(missing_ok=True)
        return await self.queue.result(job.id)

    def job_for(self, row: AndroidBuild, app: str) -> HostJob:
        """The host job that builds this version with the admin's build image."""
        build = BuildWith(image=self.settings.hosting.android_image,
                          version_code=row.version_code, version_name=row.version_name)  # fmt: skip
        return HostJob(id=secrets.token_hex(16), kind="android", app=app, environment="staging",
                       android=build, timeout_s=JOB_TIMEOUT_S)  # fmt: skip

    async def remember(self, build_id: str, job_id: str, package: str) -> None:
        async with self.db.session() as session, session.begin():
            row = await session.get(AndroidBuild, build_id)
            if row is not None:
                row.job_id, row.package_name, row.updated_at = job_id, package, time.time()

    async def ended(self, build_id: str, result: JobResult) -> None:
        """Record how the build went; a bundle counts only with the host's success."""
        async with self.db.session() as session, session.begin():
            row = await session.get(AndroidBuild, build_id)
            if row is None:
                return
            bundle = self.bundle(row)
            made = result.ok and is_bundle(bundle)
            row.status = "built" if made else "failed"
            row.error = "" if made else (result.error or "the host sent no Android bundle")
            row.hint, row.log_tail, row.updated_at = result.hint, result.log_tail, time.time()
            row.size = bundle.stat().st_size if made else 0
        if not made:
            bundle.unlink(missing_ok=True)
