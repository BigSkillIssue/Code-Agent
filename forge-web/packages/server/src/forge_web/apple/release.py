"""Releases to TestFlight (W22b): an approved commit, built, signed and uploaded, step by step.

Each release is one platform ("ios" carries iPhone, iPad and the Watch app; "macos" the Mac app)
and keeps its place and what each step left in `apple_releases`, so after a restart it goes on
where it stopped, and a failed one starts again at the step that failed. The steps: the commit
archived in the project's VM (S62) → its bundles and version read → certificates and profiles
made, and the archive signed in a fresh VM (W22b1) → the .ipa or .pkg uploaded by the server →
Apple's processing → an internal TestFlight group. The API key is used only here.
"""

import asyncio
import base64
import contextlib
import json
import logging
import shutil
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from sqlalchemy import func, select

from forge_macworker.wire import BuildParams, JobResult
from forge_sandbox.mux import ChannelClosed
from forge_sandbox.rpc import RpcError
from forge_web.apple import upload
from forge_web.apple.archive_info import ArchiveProblem, archive_info, product_of
from forge_web.apple.asc_client import AscClient, AscError, asc_client_of
from forge_web.apple.jobs import ARCHIVE, AppleJobs, NewJob
from forge_web.apple.signing import Owner, SigningProblem, registered, signing_for
from forge_web.containers.driver import SandboxError
from forge_web.db.engine import Database
from forge_web.db.models import AppleRelease
from forge_web.settings import WebSettings
from forge_web.vault import Vault

log = logging.getLogger(__name__)
SandboxCall = Callable[[str, str, dict[str, Any]], Awaitable[Any]]
STEPS = ("archive", "identify", "export", "upload", "process", "testflight", "done")
PART = 512 * 1024  # bytes per sandbox message while the commit's files come over
SANDBOX_ERRORS = (RpcError, ChannelClosed, SandboxError, OSError, TimeoutError)


class ReleaseProblem(Exception):
    """A step cannot go on; with a hint for the user."""

    def __init__(self, message: str, hint: str = "") -> None:
        super().__init__(message)
        self.hint = hint


def build_number_after(last: int, now: float) -> int:
    """The next build number: the minutes since 1970, and always more than the last one (Apple
    takes each number only once and only going up)."""
    return max(int(now // 60), last + 1)


def view(row: AppleRelease) -> dict[str, Any]:
    """A release as the page shows it."""
    data = json.loads(row.data or "{}")
    return {"id": row.id, "platform": row.platform, "commit": row.commit, "step": row.step,
            "status": row.status, "error": row.error, "hint": row.hint,
            "build_number": row.build_number, "version": data.get("version", ""),
            "bundle_id": data.get("bundle_id", ""), "steps": list(STEPS),
            "created_at": row.created_at, "updated_at": row.updated_at}  # fmt: skip


class Releases:
    """Runs the releases, one task each."""

    def __init__(
        self, db: Database, vault: Vault, settings: WebSettings, jobs: AppleJobs, call: SandboxCall
    ) -> None:
        self.db, self.vault, self.settings, self.jobs, self.call = db, vault, settings, jobs, call
        self.root = settings.data_dir / "apple" / "releases"
        self.tasks: dict[str, asyncio.Task[None]] = {}
        self.packing: dict[str, asyncio.Lock] = {}  # the sandbox packs into one file at a time
        # One signing set-up per user and team at a time: the iPhone and the Mac release would
        # otherwise each make a certificate, and Apple allows only a few.
        self.signing: dict[tuple[str, str], asyncio.Lock] = {}

    async def start(self) -> None:
        """Go on with the releases a restart interrupted."""
        async with self.db.session() as session:
            ids = list(await session.scalars(
                select(AppleRelease.id).where(AppleRelease.status == "running")))  # fmt: skip
        for release_id in ids:
            self.launch(release_id)

    async def close(self) -> None:
        """Stop the tasks; their releases stay "running" and go on after the next start."""
        tasks = list(self.tasks.values())
        for task in tasks:
            task.cancel()
        for task in tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task

    async def next_build_number(self, project_id: str, platform: str) -> int:
        async with self.db.session() as session:
            last = await session.scalar(
                select(func.max(AppleRelease.build_number)).where(
                    AppleRelease.project_id == project_id, AppleRelease.platform == platform)
            )  # fmt: skip
        return build_number_after(int(last or 0), time.time())

    def launch(self, release_id: str) -> None:
        """Run (or go on with) a release in the background."""
        if release_id in self.tasks:
            return
        task = asyncio.create_task(self.run(release_id))
        self.tasks[release_id] = task
        task.add_done_callback(lambda _: self.tasks.pop(release_id, None))

    async def run(self, release_id: str) -> None:
        """Step after step until done or failed."""
        asc: AscClient | None = None
        try:
            async with self.db.session() as session:
                row = await session.get(AppleRelease, release_id)
            if row is None or row.status != "running":
                return
            asc = await asc_client_of(self.db, self.vault, self.settings.apple.asc_api_url,
                                      row.user_id)  # fmt: skip
            if asc is None:
                raise ReleaseProblem(
                    "there is no App Store Connect key",
                    "add your team key in Settings → App Store Connect",
                )
            await self.steps(row, asc)
        except (ReleaseProblem, SigningProblem) as err:
            await self.failed(release_id, str(err), err.hint)
        except AscError as err:
            await self.failed(release_id, f"App Store Connect: {err}", apple_hint(err.status))
        except ArchiveProblem as err:
            await self.failed(release_id, str(err), "build the app again and approve it again")
        except asyncio.CancelledError:
            raise
        except Exception as err:  # a bug: the release must still not stay "running" forever
            log.exception("release %s failed", release_id)
            await self.failed(release_id, f"the release stopped: {err!r}"[:2000])
        finally:
            if asc is not None:
                await asc.close()

    async def steps(self, row: AppleRelease, asc: AscClient) -> None:
        data: dict[str, Any] = json.loads(row.data or "{}")
        folder = self.root / row.id
        step = row.step
        while step != "done":
            work = getattr(self, f"step_{step}")
            await work(row, data, asc, folder)
            step = STEPS[STEPS.index(step) + 1]
            await self.save(row.id, step, data, status="done" if step == "done" else "running")
        await asyncio.to_thread(shutil.rmtree, folder, True)

    async def save(self, release_id: str, step: str, data: dict[str, Any], status: str) -> None:
        async with self.db.session() as session, session.begin():
            row = await session.get(AppleRelease, release_id)
            if row is not None:
                row.step, row.status, row.data = step, status, json.dumps(data)
                row.error, row.hint, row.updated_at = "", "", time.time()

    async def failed(self, release_id: str, error: str, hint: str = "") -> None:
        async with self.db.session() as session, session.begin():
            row = await session.get(AppleRelease, release_id)
            if row is not None:
                row.status, row.error, row.hint = "failed", error[:4000], hint[:1000]
                row.updated_at = time.time()

    # the steps ------------------------------------------------------------------------------

    async def step_archive(
        self, row: AppleRelease, data: dict[str, Any], _asc: AscClient, folder: Path
    ) -> None:
        """The approved commit, archived for the App Store with this release's build number."""
        source = await self.fetch_commit(row.project_id, row.commit, folder / "commit.tar.gz")
        params = BuildParams(platform=row.platform, action="archive",
                             build_number=row.build_number)  # fmt: skip
        result = await self.jobs.run(NewJob(row.project_id, row.id, row.user_id, params), source)
        built = result.build
        if not result.ok or built is None or not built.ok or not built.artifact:
            hint = result.hint or "the build log is on the approval page"
            raise ReleaseProblem("the archive could not be made: " + job_failure(result), hint)
        data["archive_job"] = built.artifact.removeprefix("job:")

    async def step_identify(
        self, row: AppleRelease, data: dict[str, Any], asc: AscClient, _folder: Path
    ) -> None:
        """Which app it is (its record in App Store Connect) and which bundles it holds; the
        bundle IDs are registered here, so the user can make the app record with them."""
        info = await asyncio.to_thread(archive_info, self.job_file(data["archive_job"]))
        if info.build != str(row.build_number) or not info.version:
            raise ReleaseProblem(f"the archive is version {info.version!r} build {info.build!r}, "
                                 f"not build {row.build_number}")  # fmt: skip
        for identifier in info.bundles:  # registered first: the app record in App Store
            await registered(asc, identifier, row.platform)  # Connect can only use those
        apps = await asc.get("/v1/apps", **{"filter[bundleId]": info.bundle_id})
        found = [a for a in apps.get("data", [])
                 if a.get("attributes", {}).get("bundleId") == info.bundle_id]  # fmt: skip
        if not found:
            raise ReleaseProblem(
                f"App Store Connect has no app with the bundle ID {info.bundle_id}",
                "create the app once in App Store Connect (My Apps → +) with this bundle ID, "
                "then start the release again",
            )
        data.update(app_id=str(found[0]["id"]), bundle_id=info.bundle_id, version=info.version,
                    bundles=list(info.bundles))  # fmt: skip

    async def step_export(
        self, row: AppleRelease, data: dict[str, Any], asc: AscClient, folder: Path
    ) -> None:
        """Certificates and profiles made, and the archive signed in a fresh VM (the signing
        material exists only in memory, for this one job)."""
        owner = Owner(self.db, self.vault, row.user_id, asc.key.team_id)
        async with self.signing.setdefault((owner.user_id, owner.team_id), asyncio.Lock()):
            params, material = await signing_for(owner, asc, row.platform, tuple(data["bundles"]))
        folder.mkdir(parents=True, exist_ok=True)
        source = folder / "archive.tar.gz"
        await asyncio.to_thread(shutil.copyfile, self.job_file(data["archive_job"]), source)
        job = NewJob(row.project_id, row.id, row.user_id, params, signing=material)
        result = await self.jobs.run(job, source)
        exported = result.export
        if not result.ok or exported is None or not exported.ok or not exported.artifact:
            tail = exported.log_tail[-1500:] if exported is not None else ""
            raise ReleaseProblem("the archive could not be signed: " + job_failure(result, tail),
                                 "the certificates or profiles may not fit the app")  # fmt: skip
        data["export_job"] = exported.artifact.removeprefix("job:")

    async def step_upload(
        self, row: AppleRelease, data: dict[str, Any], asc: AscClient, folder: Path
    ) -> None:
        """The .ipa or .pkg to App Store Connect, by the server."""
        if data.get("upload_id"):  # a restart during the upload: go on with the same one
            state, _, reasons = await upload.upload_state(asc, data["upload_id"])
            if state in ("PROCESSING", "COMPLETE"):
                return
            if state == "FAILED":
                raise ReleaseProblem(f"Apple refused the upload: {reasons or 'no reason given'}")
        else:
            data["upload_id"] = await upload.start_upload(
                asc, data["app_id"], row.platform, data["version"], str(row.build_number)
            )
            await self.save(row.id, "upload", data, status="running")
        product = await asyncio.to_thread(product_of, self.job_file(data["export_job"]),
                                          row.platform, folder / "product")  # fmt: skip
        await upload.send_file(asc, data["upload_id"], row.platform, product)

    async def step_process(
        self, row: AppleRelease, data: dict[str, Any], asc: AscClient, _folder: Path
    ) -> None:
        """Wait for Apple to accept the upload and process the build."""
        data.setdefault("process_started", time.time())
        poll = self.settings.apple.release_poll_s
        while not data.get("build_id"):
            state, build_id, reasons = await upload.upload_state(asc, data["upload_id"])
            if state == "FAILED":
                raise ReleaseProblem(f"Apple refused the upload: {reasons or 'no reason given'}")
            if state == "COMPLETE" and build_id:
                data["build_id"] = build_id
                await self.save(row.id, "process", data, status="running")
                break
            await self.wait(data, poll)
        while (state := await upload.build_state(asc, data["build_id"])) != "VALID":
            if state in ("INVALID", "FAILED"):
                raise ReleaseProblem(
                    "Apple found a problem with the build",
                    "App Store Connect sent the team an email with the details",
                )
            await self.wait(data, poll)

    async def step_testflight(
        self, row: AppleRelease, data: dict[str, Any], asc: AscClient, _folder: Path
    ) -> None:
        """An internal TestFlight group that gets every build."""
        data["group_id"] = await upload.testers_group(asc, data["app_id"])

    # helpers ----------------------------------------------------------------------------------

    async def wait(self, data: dict[str, Any], poll: float) -> None:
        if time.time() - data["process_started"] > self.settings.apple.processing_timeout_s:
            raise ReleaseProblem(
                "Apple did not finish processing the build in time",
                "look in App Store Connect (TestFlight), then try again",
            )
        await asyncio.sleep(poll)

    def job_file(self, job_id: str) -> Path:
        path = self.jobs.root / job_id / ARCHIVE
        if not path.is_file():
            raise ReleaseProblem("what the Mac made is no longer on the server",
                                 "start the release again")  # fmt: skip
        return path

    async def fetch_commit(self, project_id: str, commit: str, target: Path) -> Path:
        """The commit's files, packed by the project's sandbox and read over in parts."""
        target.parent.mkdir(parents=True, exist_ok=True)
        async with self.packing.setdefault(project_id, asyncio.Lock()):
            return await self.read_commit(project_id, commit, target)

    async def read_commit(self, project_id: str, commit: str, target: Path) -> Path:
        limit = self.settings.apple.max_source_mb * 1024 * 1024
        try:
            packed = await self.call(project_id, "git.archive", {"commit": commit})
            path = str(packed["path"])
            if int(packed.get("size", 0)) > limit:
                raise ReleaseProblem("the project is larger than a Mac job may be",
                                     f"at most {self.settings.apple.max_source_mb} MB")  # fmt: skip
            with target.open("wb") as out:
                offset = 0
                while True:
                    part = await self.call(
                        project_id, "fs.read", {"path": path, "offset": offset, "limit": PART}
                    )
                    chunk = part_bytes(part)
                    out.write(chunk)
                    offset += len(chunk)
                    if not part.get("truncated") or not chunk or offset > limit:
                        break
            with contextlib.suppress(*SANDBOX_ERRORS):
                await self.call(project_id, "fs.delete", {"path": path})
        except SANDBOX_ERRORS as err:
            raise ReleaseProblem(
                f"the project's sandbox could not pack the commit: {err}",
                "open the project once so its sandbox starts",
            ) from None
        return target


def part_bytes(part: dict[str, Any]) -> bytes:
    """The bytes of one read result (text or base64)."""
    if isinstance(part.get("base64"), str):
        return base64.b64decode(part["base64"])
    return str(part.get("text") or "").encode("utf-8")


def job_failure(result: JobResult, tail: str = "") -> str:
    """Why a Mac job did not deliver, in a few lines."""
    if not result.ok:
        return result.error or "the Mac could not run the job"
    issues = result.build.issues if result.build is not None else []
    errors = [i.message for i in issues if i.severity == "error"][:5]
    return "; ".join(errors) or tail or "no reason given"


def apple_hint(status: int) -> str:
    """What to do about an App Store Connect error."""
    if status == 401:
        return "the App Store Connect key no longer works: check it in Settings"
    if status == 403:
        return "the key needs the Admin role in App Store Connect to make certificates"
    if status == 409:
        return "App Store Connect refused it as it is; Apple's words above say why"
    return "try again later; Apple may be busy"
