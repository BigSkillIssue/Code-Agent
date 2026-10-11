"""Operations of hosted apps (W29a): backups that leave the app servers, and restore drills.

Every live app and environment is backed up every `backup_every_h` hours: its host packs the
database and the files, encrypts them with the app's own key (made here, kept in the vault and
sealed to the host for each job) and uploads only the encrypted file, which is kept in
`<data>/hosting/backups/` (the newest `keep_backups`). Every `drill_every_days` the newest backup
goes back to the host, which restores it into a throwaway database: a backup counts only once
it was read back.
"""

import asyncio
import base64
import contextlib
import json
import logging
import os
import secrets
import time
from collections.abc import Awaitable
from pathlib import Path

from sqlalchemy import select

from forge_hostworker.wire import HostJob, JobResult, SealedSecrets, seal
from forge_web.db.engine import Database
from forge_web.db.hosting_models import (
    AppBackup,
    AppBackupKey,
    Deploy,
    HostedApp,
    HostJobRow,
    HostServer,
)
from forge_web.hosting.hosts import HostQueue
from forge_web.settings import WebSettings
from forge_web.vault import Vault

log = logging.getLogger(__name__)
TICK_S = 600.0  # how often the schedule is looked at
KEY_SECRET = "BACKUP_KEY"  # as forge_hostworker.backup names it
JOB_TIMEOUT_S = 3 * 3600.0


class Backups:
    """Backs up the live apps on schedule and runs the restore drills."""

    def __init__(self, db: Database, vault: Vault, settings: WebSettings, queue: HostQueue):
        self.db, self.vault, self.settings, self.queue = db, vault, settings, queue
        self.root = settings.data_dir / "hosting" / "backups"
        self.busy: set[tuple[str, str]] = set()  # app and environment with a job under way
        self.task: asyncio.Task[None] | None = None

    def start(self) -> None:
        """Look at the schedule now and then, in the background."""
        self.task = asyncio.create_task(self.keep_going())

    async def close(self) -> None:
        if self.task is not None:
            self.task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.task

    async def keep_going(self) -> None:
        while True:
            try:
                await self.tick()
            except Exception:  # a bad tick must not end the schedule
                log.exception("the backup schedule failed")
            await asyncio.sleep(TICK_S)

    async def tick(self) -> list[asyncio.Task[str]]:
        """Start the backups and drills that are due; their tasks."""
        hosting, now, started = self.settings.hosting, time.time(), []
        for project_id, environment in await self.live():
            if (project_id, environment) in self.busy:
                continue
            newest = await self.newest(project_id, environment)
            if newest is None or now - newest.created_at > hosting.backup_every_h * 3600:
                started.append(asyncio.create_task(self.backup(project_id, environment)))
            elif now - newest.drill_at > hosting.drill_every_days * 86400:
                started.append(asyncio.create_task(self.drill(project_id, environment)))
        return started

    async def live(self) -> list[tuple[str, str]]:
        """The apps and environments with a live release (not suspended or being deleted)."""
        async with self.db.session() as session:
            rows = await session.execute(
                select(Deploy.project_id, Deploy.environment)
                .join(HostedApp, HostedApp.project_id == Deploy.project_id)
                .where(Deploy.status == "live", HostedApp.suspended.is_(False),
                       HostedApp.deleting_at == 0)
            )  # fmt: skip
            return sorted({(project, environment) for project, environment in rows})

    async def newest(self, project_id: str, environment: str) -> AppBackup | None:
        async with self.db.session() as session:
            return await session.scalar(
                select(AppBackup).where(AppBackup.project_id == project_id,
                                        AppBackup.environment == environment)
                .order_by(AppBackup.created_at.desc()).limit(1)
            )  # fmt: skip

    def path(self, project_id: str, environment: str, backup_id: str) -> Path:
        return self.root / project_id / environment / f"{backup_id}.fbk"

    async def target_of(self, job_id: str) -> Path:
        """Where an archive job's upload is kept."""
        async with self.db.session() as session:
            row = await session.get(HostJobRow, job_id)
            hosted = (
                await session.scalar(select(HostedApp).where(HostedApp.app == row.app))
                if row
                else None
            )
        if row is None or hosted is None:
            raise LookupError("no such archive job")
        environment = HostJob.model_validate_json(row.job).environment
        return self.path(hosted.project_id, environment, job_id)

    async def backup(self, project_id: str, environment: str) -> str:
        """One backup now; "" when it worked, else why not."""
        self.busy.add((project_id, environment))
        try:
            hosted, sealed = await self.sealed_key(project_id)
            job = HostJob(id=secrets.token_hex(16), kind="archive", app=hosted.app,
                          environment=environment, timeout_s=JOB_TIMEOUT_S)  # fmt: skip
            target = self.path(project_id, environment, job.id)
            await self.queue.send(hosted.host_id, "", job, sealed=sealed)
            result = await self.queue.result(job.id)
            if not result.ok or not target.is_file():
                return result.error or "the host sent no backup"
            await self.keep(project_id, environment, job.id, target.stat().st_size)
            return ""
        finally:
            self.busy.discard((project_id, environment))

    async def drill(self, project_id: str, environment: str) -> str:
        """Restore the newest backup on the app's host; "" when it could be read back."""
        newest = await self.newest(project_id, environment)
        if newest is None:
            return "there is no backup yet"
        self.busy.add((project_id, environment))
        try:
            hosted, sealed = await self.sealed_key(project_id)
            job = HostJob(id=secrets.token_hex(16), kind="restore_drill", app=hosted.app,
                          environment=environment, timeout_s=JOB_TIMEOUT_S)  # fmt: skip
            source = self.path(project_id, environment, newest.id)
            await self.queue.send(hosted.host_id, "", job, source, sealed)
            result = await self.queue.result(job.id)
            await self.drilled(newest.id, result)
            return "" if result.ok else result.error or "the drill failed"
        finally:
            self.busy.discard((project_id, environment))

    async def sealed_key(self, project_id: str) -> tuple[HostedApp, SealedSecrets]:
        """The app and its backup key, sealed to its host (the key is made once)."""
        async with self.db.session() as session, session.begin():
            hosted = await session.get(HostedApp, project_id)
            host = await session.get(HostServer, hosted.host_id) if hosted else None
            if hosted is None or host is None:
                raise LookupError("the app has no host")
            row = await session.get(AppBackupKey, project_id)
            if row is None:
                key = base64.b64encode(os.urandom(32)).decode()
                row = AppBackupKey(project_id=project_id, key=self.vault.encrypt(key),
                                   created_at=time.time())  # fmt: skip
                session.add(row)
            else:
                key = self.vault.decrypt(row.key)
        return hosted, seal(host.public_key, json.dumps({KEY_SECRET: key}).encode())

    async def keep(self, project_id: str, environment: str, backup_id: str, size: int) -> None:
        """Record a new backup and drop the oldest beyond `keep_backups`."""
        async with self.db.session() as session, session.begin():
            session.add(AppBackup(id=backup_id, project_id=project_id, environment=environment,
                                  size=size, created_at=time.time()))  # fmt: skip
            await session.flush()
            rows = list(await session.scalars(
                select(AppBackup).where(AppBackup.project_id == project_id,
                                        AppBackup.environment == environment)
                .order_by(AppBackup.created_at.desc())))  # fmt: skip
            for old in rows[self.settings.hosting.keep_backups :]:
                await session.delete(old)
                await asyncio.to_thread(
                    self.path(project_id, environment, old.id).unlink, missing_ok=True
                )

    async def drilled(self, backup_id: str, result: JobResult) -> None:
        """Record how the drill went."""
        detail = result.checks[0].detail if result.checks else result.error
        async with self.db.session() as session, session.begin():
            row = await session.get(AppBackup, backup_id)
            if row is not None:
                row.drill_at, row.drill_ok, row.drill_detail = time.time(), result.ok, detail[:2000]


async def said(work: Awaitable[str]) -> None:
    """Run a backup or drill on the side; what went wrong goes to the log."""
    problem = await work
    if problem:
        log.warning("hosting: %s", problem)
