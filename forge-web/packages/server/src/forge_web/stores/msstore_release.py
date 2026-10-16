"""Microsoft Store submissions (W38b): a Windows build of an approved commit, submitted to the
app's Store listing in Partner Center with its maker's Entra ID app.

Like a production deploy, a submission goes out only after the creator's start and an admin's
approval (trusted users and admins need none). Once committed, Microsoft checks and certifies
it; Forge Web keeps following its status when the page asks (at most once a minute).
"""

import asyncio
import contextlib
import logging
import time
from typing import Any

from sqlalchemy import select

from forge_web.db.engine import Database
from forge_web.db.store_models import MsStoreKey, MsSubmission, WindowsBuild
from forge_web.settings import WebSettings
from forge_web.stores.msstore import EntraApp, StoreClient, StoreError
from forge_web.stores.windows import WindowsBuilds
from forge_web.vault import Vault

log = logging.getLogger(__name__)
ACTIVE = ("waiting", "submitting")
FOLLOW_EVERY_S = 60.0
FAILED = ("CommitFailed", "PreProcessingFailed", "CertificationFailed", "ReleaseFailed",
          "PublishFailed", "Canceled")  # fmt: skip
HINTS = {
    "in progress in Partner Center": "Finish or delete the open submission in Partner Center "
    "first, then submit again.",
    "Forbidden": "In Partner Center (Account settings, User management, Microsoft Entra "
    "applications), add the Entra ID app as a Manager.",
    "not found": "Create the app in Partner Center and give its Store ID here.",
    "invalid_client": "The client secret is wrong or expired: make a new one in Entra ID.",
}


def view(row: MsSubmission, build: WindowsBuild | None = None) -> dict[str, Any]:
    """A submission as the pages show it."""
    shown = {"id": row.id, "build_id": row.build_id, "store_id": row.store_id,
             "status": row.status, "store_status": row.store_status,
             "submission_id": row.submission_id, "admin": row.admin,
             "admin_approved_at": row.admin_approved_at, "error": row.error, "hint": row.hint,
             "created_at": row.created_at, "updated_at": row.updated_at}  # fmt: skip
    if build is not None:
        shown |= {"version": build.version, "file_name": build.file_name}
    return shown


def hint_for(error: StoreError) -> str:
    """What the user can do about Microsoft's refusal."""
    if error.status == 403:
        return HINTS["Forbidden"]
    return next((hint for words, hint in HINTS.items() if words in str(error)), "")


async def store_client_of(
    db: Database, vault: Vault, settings: WebSettings, user_id: str
) -> StoreClient | None:
    """A client that signs in as this user's Entra ID app, or None when the user has none."""
    async with db.session() as session:
        row = await session.get(MsStoreKey, user_id)
    if row is None:
        return None
    app = EntraApp.model_validate_json(vault.decrypt(row.secret))
    stores = settings.stores
    return StoreClient(stores.msstore_api_url, stores.msstore_login_url, app)


class StoreSubmissions:
    """Submits, one task per submission, and follows what Microsoft makes of it."""

    def __init__(
        self, db: Database, vault: Vault, settings: WebSettings, builds: WindowsBuilds
    ) -> None:
        self.db, self.vault, self.settings, self.builds = db, vault, settings, builds
        self.tasks: dict[str, asyncio.Task[None]] = {}

    async def start(self) -> None:
        """Go on with the submissions a restart interrupted."""
        async with self.db.session() as session:
            ids = list(await session.scalars(
                select(MsSubmission.id).where(MsSubmission.status == "submitting")))  # fmt: skip
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
        """Run a submission in the background."""
        if submission_id in self.tasks:
            return
        task = asyncio.create_task(self.run(submission_id))
        self.tasks[submission_id] = task
        task.add_done_callback(lambda _: self.tasks.pop(submission_id, None))

    async def run(self, submission_id: str) -> None:
        """Upload and commit; whatever goes wrong ends it as failed."""
        try:
            await self.submitted(submission_id)
        except StoreError as err:
            await self.update(submission_id, status="failed", error=f"Microsoft Store: {err}",
                              hint=hint_for(err))  # fmt: skip
        except asyncio.CancelledError:
            raise
        except Exception as err:  # a bug: the submission must not stay "submitting" forever
            log.exception("store submission %s failed", submission_id)
            await self.update(submission_id, status="failed",
                              error=f"the submission stopped: {err!r}"[:2000])  # fmt: skip

    async def submitted(self, submission_id: str) -> None:
        async with self.db.session() as session:
            row = await session.get(MsSubmission, submission_id)
            build = await session.get(WindowsBuild, row.build_id) if row else None
        if row is None or build is None:
            raise StoreError(0, "the submission or its build is gone")
        client = await store_client_of(self.db, self.vault, self.settings, row.user_id)
        if client is None:
            raise StoreError(0, "the Entra ID app was removed")
        try:
            made = await client.submit(row.store_id, self.builds.package(build))
            await self.update(submission_id, status="submitted", submission_id=made,
                              followed_at=0.0)  # fmt: skip
        finally:
            await client.close()
        await self.follow(submission_id)

    async def follow(self, submission_id: str) -> None:
        """Ask Microsoft how a submitted submission goes (at most once a minute)."""
        async with self.db.session() as session:
            row = await session.get(MsSubmission, submission_id)
        if row is None or row.status != "submitted":
            return
        if time.time() - row.followed_at < FOLLOW_EVERY_S:
            return
        client = await store_client_of(self.db, self.vault, self.settings, row.user_id)
        if client is None:
            return
        try:
            status, errors = await client.status(row.store_id, row.submission_id)
        except StoreError as err:
            log.warning("following submission %s: %s", submission_id, err)
            return
        finally:
            await client.close()
        failed = status in FAILED
        await self.update(submission_id, store_status=status, followed_at=time.time(),
                          status="failed" if failed else "submitted",
                          error="; ".join(errors)[:2000] if failed else "")  # fmt: skip

    async def update(self, row_id: str, **values: Any) -> None:
        async with self.db.session() as session, session.begin():
            row = await session.get(MsSubmission, row_id)
            if row is not None:
                for name, value in values.items():
                    setattr(row, name, value)
                row.updated_at = time.time()
