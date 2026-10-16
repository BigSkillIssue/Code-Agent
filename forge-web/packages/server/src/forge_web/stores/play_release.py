"""Google Play releases (W37b): an Android build of an approved commit, signed with the app's
upload key and released on a Play track with its maker's service account.

Like a production deploy, a release goes out only after the creator's start and an admin's
approval (trusted users and admins need none). Google keeps an app's first release as a draft
until its store listing is done in Play Console; Forge Web then saves the release as a draft
and says so.
"""

import asyncio
import contextlib
import logging
import time
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from forge_web.db.engine import Database
from forge_web.db.models import Project
from forge_web.db.store_models import AndroidBuild, PlayKey, PlayRelease, PlayUploadKey
from forge_web.settings import WebSettings
from forge_web.stores.android import AndroidBuilds
from forge_web.stores.play import PlayClient, PlayError, ServiceAccount
from forge_web.stores.play_signing import fingerprint, new_upload_key, sign_bundle
from forge_web.vault import Vault

log = logging.getLogger(__name__)
ACTIVE = ("waiting", "releasing")
DRAFT_HINT = (
    "Google Play keeps an app's first release as a draft: finish its store listing, content "
    "rating and app content in Play Console and send the release for review there; later "
    "releases go out from here."
)
HINTS = {
    "wrong key": "Google Play knows another upload key for this app: in Play Console (Test and "
    "release, App integrity) ask for an upload key reset with this app's certificate from Forge "
    "Web.",
    "does not have permission": "In Play Console (Users and permissions), invite the service "
    "account's email for this app with the permission to release.",
    "Package not found": "Create the app in Play Console with this application ID first.",
    "closed test": "Release to the closed test (alpha) first.",
}


def view(row: PlayRelease, build: AndroidBuild | None = None) -> dict[str, Any]:
    """A release as the pages show it."""
    shown = {"id": row.id, "build_id": row.build_id, "track": row.track, "status": row.status,
             "admin": row.admin, "admin_approved_at": row.admin_approved_at, "notes": row.notes,
             "error": row.error, "hint": row.hint, "created_at": row.created_at,
             "updated_at": row.updated_at}  # fmt: skip
    if build is not None:
        shown |= {"version_code": build.version_code, "version_name": build.version_name,
                  "package_name": build.package_name}  # fmt: skip
    return shown


def hint_for(error: PlayError) -> str:
    """What the user can do about Google's refusal."""
    return next((hint for words, hint in HINTS.items() if words in str(error)), "")


async def play_client_of(
    db: Database, vault: Vault, settings: WebSettings, user_id: str
) -> tuple[PlayClient, PlayKey] | None:
    """A client that signs with this user's service account, or None when the user has none."""
    async with db.session() as session:
        row = await session.get(PlayKey, user_id)
    if row is None:
        return None
    account = ServiceAccount.model_validate_json(vault.decrypt(row.secret))
    stores = settings.stores
    return PlayClient(stores.play_api_url, stores.play_token_url, account), row


class PlayReleases:
    """Signs and releases, one task per release."""

    def __init__(
        self, db: Database, vault: Vault, settings: WebSettings, builds: AndroidBuilds
    ) -> None:
        self.db, self.vault, self.settings, self.builds = db, vault, settings, builds
        self.tasks: dict[str, asyncio.Task[None]] = {}

    async def start(self) -> None:
        """Go on with the releases a restart interrupted."""
        async with self.db.session() as session:
            ids = list(await session.scalars(
                select(PlayRelease.id).where(PlayRelease.status == "releasing")))  # fmt: skip
        for release_id in ids:
            self.launch(release_id)

    async def close(self) -> None:
        tasks = list(self.tasks.values())
        for task in tasks:
            task.cancel()
        for task in tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task

    def launch(self, release_id: str) -> None:
        """Run a release in the background."""
        if release_id in self.tasks:
            return
        task = asyncio.create_task(self.run(release_id))
        self.tasks[release_id] = task
        task.add_done_callback(lambda _: self.tasks.pop(release_id, None))

    async def upload_key(self, project_id: str) -> tuple[str, str]:
        """The app's upload key and certificate (PEM), made the first time they are needed."""
        async with self.db.session() as session:
            row = await session.get(PlayUploadKey, project_id)
            project = await session.get(Project, project_id)
        if row is not None:
            return self.vault.decrypt(row.secret), row.certificate
        name = project.name if project is not None else project_id
        key, certificate = await asyncio.to_thread(new_upload_key, name)
        try:
            async with self.db.session() as session, session.begin():
                sealed = self.vault.encrypt(key)
                session.add(
                    PlayUploadKey(
                        project_id=project_id,
                        secret=sealed,
                        certificate=certificate,
                        created_at=time.time(),
                    )
                )
        except IntegrityError:  # made at the same moment by another request: use that one
            return await self.upload_key(project_id)
        return key, certificate

    async def certificate(self, project_id: str) -> dict[str, str]:
        """The upload certificate as Play Console asks for it."""
        _, certificate = await self.upload_key(project_id)
        return {"certificate": certificate, "sha256": fingerprint(certificate)}

    async def signed(self, build: AndroidBuild) -> Path:
        """The build's bundle signed with the app's upload key (signed once, then kept)."""
        unsigned = self.builds.bundle(build)
        target = unsigned.with_suffix(".signed.aab")
        if not target.is_file():
            key, certificate = await self.upload_key(build.project_id)
            await asyncio.to_thread(sign_bundle, unsigned, target, key, certificate)
        return target

    async def run(self, release_id: str) -> None:
        """The release from signing to Google's answer; whatever goes wrong ends it as failed."""
        try:
            status, hint = await self.released(release_id)
            await self.ended(release_id, status, "", hint)
        except PlayError as err:
            await self.ended(release_id, "failed", f"Google Play: {err}", hint_for(err))
        except asyncio.CancelledError:
            raise
        except Exception as err:  # a bug: the release must still not stay "releasing" forever
            log.exception("play release %s failed", release_id)
            await self.ended(release_id, "failed", f"the release stopped: {err!r}"[:2000], "")

    async def released(self, release_id: str) -> tuple[str, str]:
        """Release the build on its track; ("released" or "draft", a hint)."""
        async with self.db.session() as session:
            row = await session.get(PlayRelease, release_id)
            build = await session.get(AndroidBuild, row.build_id) if row else None
        if row is None or build is None:
            raise PlayError(0, "the release or its build is gone")
        found = await play_client_of(self.db, self.vault, self.settings, row.user_id)
        if found is None:
            raise PlayError(0, "the Google Play service account was removed")
        client, _ = found
        bundle = await self.signed(build)
        name, code = build.version_name, build.version_code
        try:
            try:
                await client.release(build.package_name, bundle, row.track, code, name, row.notes)
                return "released", ""
            except PlayError as err:
                if "draft app" not in str(err):
                    raise
            await client.release(build.package_name, bundle, row.track, code, name, row.notes,
                                 status="draft")  # fmt: skip
            return "draft", DRAFT_HINT
        finally:
            await client.close()

    async def ended(self, release_id: str, status: str, error: str, hint: str) -> None:
        async with self.db.session() as session, session.begin():
            row = await session.get(PlayRelease, release_id)
            if row is not None:
                row.status, row.error, row.hint = status, error[:2000], hint
                row.updated_at = time.time()
