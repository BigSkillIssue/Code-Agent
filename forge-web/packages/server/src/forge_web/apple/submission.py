"""Submissions to the App Store (W22d2): a release that reached TestFlight, made ready for App
Review on the user's click, submitted only on a second, confirmed click, followed through App
Review, and released on a third.

Like releases, each submission keeps its place in `apple_submissions`, so a restart goes on
where it stopped. Preparing changes only the version in preparation in App Store Connect;
nothing reaches App Review before the user confirms.
"""

import asyncio
import contextlib
import json
import logging
import shutil
import time
from dataclasses import dataclass
from typing import Any

from forge.apple_listing import StoreListing
from sqlalchemy import select

from forge_web.apple import store_media, submit
from forge_web.apple.asc_client import AscClient, AscError, asc_client_of
from forge_web.apple.jobs import NewJob
from forge_web.apple.release import Releases, apple_hint
from forge_web.db.models import AppleListing, AppleRelease, AppleSubmission

log = logging.getLogger(__name__)
STEPS = ("screenshots", "version", "texts", "terms", "contact", "placements")
FOLLOWED = ("submitted",)  # App Review is asked about these until it decides
DECIDED = ("PENDING_DEVELOPER_RELEASE", "READY_FOR_DISTRIBUTION", "READY_FOR_SALE", "REJECTED",
           "METADATA_REJECTED", "DEVELOPER_REJECTED", "INVALID_BINARY")  # fmt: skip


def view(row: AppleSubmission) -> dict[str, Any]:
    """A submission as the page shows it."""
    data = json.loads(row.data or "{}")
    return {"id": row.id, "release_id": row.release_id, "platform": row.platform,
            "version": row.version, "status": row.status, "step": row.step, "steps": list(STEPS),
            "error": row.error, "hint": row.hint, "review_state": data.get("review_state", ""),
            "version_state": data.get("version_state", ""),
            "screenshots": len(data.get("shots", [])), "created_at": row.created_at,
            "updated_at": row.updated_at}  # fmt: skip


class Submissions:
    """Runs the preparations and follows App Review, one task each."""

    def __init__(self, releases: Releases) -> None:
        self.releases = releases
        self.db, self.vault, self.settings = releases.db, releases.vault, releases.settings
        self.tasks: dict[str, asyncio.Task[None]] = {}

    async def start(self) -> None:
        """Go on with what a restart interrupted."""
        async with self.db.session() as session:
            ids = list(await session.scalars(select(AppleSubmission.id).where(
                AppleSubmission.status.in_(("preparing", *FOLLOWED)))))  # fmt: skip
        for submission_id in ids:
            self.launch(submission_id)

    async def close(self) -> None:
        tasks = list(self.tasks.values())
        for task in tasks:
            task.cancel()
        for task in tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task

    def launch(self, submission_id: str) -> None:
        if submission_id in self.tasks:
            return
        task = asyncio.create_task(self.run(submission_id))
        self.tasks[submission_id] = task
        task.add_done_callback(lambda _: self.tasks.pop(submission_id, None))

    async def client(self, user_id: str) -> AscClient:
        asc = await asc_client_of(self.db, self.vault, self.settings.apple.asc_api_url, user_id)
        if asc is None:
            raise submit.SubmitProblem(
                "there is no App Store Connect key",
                "add your team key in Settings → App Store Connect",
            )
        return asc

    async def run(self, submission_id: str) -> None:
        """Prepare, or follow App Review, as the submission's status says."""
        asc: AscClient | None = None
        try:
            row = await self.row(submission_id)
            asc = await self.client(row.user_id)
            if row.status == "preparing":
                await self.prepare(row, asc)
            elif row.status in FOLLOWED:
                await self.follow(row, asc)
        except (submit.SubmitProblem, store_media.MediaProblem) as err:
            await self.save(submission_id, status="failed", error=str(err),
                            hint=getattr(err, "hint", ""))  # fmt: skip
        except AscError as err:
            await self.save(submission_id, status="failed", error=f"App Store Connect: {err}",
                            hint=apple_hint(err.status))  # fmt: skip
        except asyncio.CancelledError:
            raise
        except Exception as err:  # a bug: the submission must not stay "preparing" forever
            log.exception("submission %s failed", submission_id)
            await self.save(submission_id, status="failed", error=f"stopped: {err!r}"[:2000])
        finally:
            if asc is not None:
                await asc.close()

    async def prepare(self, row: AppleSubmission, asc: AscClient) -> None:
        """Every step from the stored one on; then the submission waits for the user."""
        release = await self.release_of(row)
        prep = Prep(
            row,
            json.loads(release.data or "{}"),
            release.commit,
            await self.listing_of(row.project_id),
            json.loads(row.data or "{}"),
            asc,
        )
        for name in STEPS[STEPS.index(row.step) :]:
            await self.save(row.id, step=name, data=prep.data)
            await getattr(self, f"step_{name}")(prep)
        await self.save(row.id, status="ready", step=STEPS[-1], data=prep.data)  # fmt: skip

    # the steps --------------------------------------------------------------------------------

    async def step_screenshots(self, prep: "Prep") -> None:
        """Light and dark store screenshots of every device, taken from the released commit."""
        if prep.data.get("shots"):
            return
        ref = (await prep.asc.get("/v1/appAssetLibraryRefData"))["data"]
        attributes = (ref[0] if isinstance(ref, list) else ref).get("attributes", {})
        row = prep.row
        families = store_media.families_for(row.platform, prep.release.get("bundles", []))
        slots = store_media.slots_from(attributes, families)
        folder = self.releases.root.parent / "submissions" / row.id
        try:
            source = await self.releases.fetch_commit(row.project_id, prep.commit,
                                                      folder / "commit.tar.gz")  # fmt: skip
            owner = NewJob(row.project_id, row.id, row.user_id, None)  # type: ignore[arg-type]
            shots = await store_media.take_shots(self.releases.jobs, owner, source, slots, folder)
            prep.data["shots"] = await store_media.upload_shots(
                prep.asc, prep.release["app_id"], shots, self.settings.apple.release_poll_s
            )
        finally:
            await asyncio.to_thread(shutil.rmtree, folder, True)

    async def step_version(self, prep: "Prep") -> None:
        """The version in preparation with the release's build."""
        app_id, platform = prep.release["app_id"], prep.row.platform
        prep.data["first"] = await submit.first_version(prep.asc, app_id, platform)
        prep.data["version_id"] = await submit.version(prep.asc, app_id, platform,
                                                       prep.row.version)  # fmt: skip
        await submit.set_version(prep.asc, prep.data["version_id"], prep.release["build_id"],
                                 prep.listing)  # fmt: skip

    async def step_texts(self, prep: "Prep") -> None:
        """The saved store texts, app information, categories and age rating."""
        prep.data["localization_id"] = await submit.localization(
            prep.asc, prep.data["version_id"], prep.listing, bool(prep.data.get("first"))
        )
        prep.data["info_id"] = await submit.app_info(prep.asc, prep.release["app_id"], prep.listing)

    async def step_terms(self, prep: "Prep") -> None:
        """Content rights, a free price and availability, where they are not set yet."""
        await submit.app_terms(prep.asc, prep.release["app_id"])

    async def step_contact(self, prep: "Prep") -> None:
        """Who App Review may contact."""
        contact = json.loads(prep.row.contact or "{}")
        await submit.review_contact(prep.asc, prep.data["version_id"], contact,
                                    contact.get("notes", ""))  # fmt: skip

    async def step_placements(self, prep: "Prep") -> None:
        """The store screenshots placed on the version, light ones first."""
        localization = prep.data["localization_id"]
        await submit.clear_screenshots(prep.asc, localization)
        ordered = sorted(prep.data["shots"], key=lambda s: (s["family"], s["dark"]))
        prep.data["placements"] = [
            await store_media.place(prep.asc, s["group"], s["family"], s["image_id"], localization)
            for s in ordered
        ]

    # after the user's confirmation ------------------------------------------------------------

    async def send(self, submission_id: str) -> None:
        """Submit to App Review now (the user confirmed it); then follow the review."""
        row = await self.row(submission_id)
        data = json.loads(row.data or "{}")
        release = await self.release_of(row)
        asc = await self.client(row.user_id)
        try:
            app_id = json.loads(release.data or "{}")["app_id"]
            data["submission_id"] = await submit.submit(asc, app_id, row.platform,
                                                        data["version_id"])  # fmt: skip
        finally:
            await asc.close()
        await self.save(row.id, status="submitted", data=data)
        self.launch(row.id)

    async def follow(self, row: AppleSubmission, asc: AscClient) -> None:
        """Ask App Review where it is until it decided."""
        data = json.loads(row.data or "{}")
        while True:
            review, state = await submit.review_state(
                asc, data["submission_id"], data["version_id"]
            )
            if (review, state) != (data.get("review_state"), data.get("version_state")):
                data.update(review_state=review, version_state=state)
                await self.save(row.id, data=data)
            if state in DECIDED:
                return
            await asyncio.sleep(self.settings.apple.review_poll_s)

    async def publish(self, submission_id: str) -> None:
        """Release an approved version on the App Store (the user's click)."""
        row = await self.row(submission_id)
        data = json.loads(row.data or "{}")
        asc = await self.client(row.user_id)
        try:
            await submit.release(asc, data["version_id"])
        finally:
            await asc.close()
        data["version_state"] = "PROCESSING_FOR_DISTRIBUTION"
        await self.save(row.id, status="released", data=data)

    # storage --------------------------------------------------------------------------------

    async def row(self, submission_id: str) -> AppleSubmission:
        async with self.db.session() as session:
            row = await session.get(AppleSubmission, submission_id)
        if row is None:
            raise submit.SubmitProblem("the submission is gone")
        return row

    async def release_of(self, row: AppleSubmission) -> AppleRelease:
        async with self.db.session() as session:
            release = await session.get(AppleRelease, row.release_id)
        if release is None or release.status != "done":
            raise submit.SubmitProblem("the release to TestFlight is gone or not done")
        return release

    async def listing_of(self, project_id: str) -> StoreListing:
        async with self.db.session() as session:
            saved = await session.get(AppleListing, project_id)
        if saved is None:
            raise submit.SubmitProblem("there are no saved store texts", "save them first")
        return StoreListing.model_validate_json(saved.data)

    async def save(self, submission_id: str, **changes: Any) -> None:
        async with self.db.session() as session, session.begin():
            row = await session.get(AppleSubmission, submission_id)
            if row is None:
                return
            if "data" in changes:
                changes["data"] = json.dumps(changes["data"])
            if changes.get("status") not in (None, "failed"):
                changes.setdefault("error", "")
                changes.setdefault("hint", "")
            for key, value in changes.items():
                setattr(row, key, value[:4000] if isinstance(value, str) else value)
            row.updated_at = time.time()


@dataclass
class Prep:
    """What the preparation's steps work with: the submission, its release's results, the
    released commit, the saved texts, what the steps left, and the client."""

    row: AppleSubmission
    release: dict[str, Any]
    commit: str
    listing: StoreListing
    data: dict[str, Any]
    asc: AscClient
