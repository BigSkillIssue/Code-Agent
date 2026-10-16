"""Deleting a hosted app completely (W29c): on its host every container, the network, the
database and its volume, the files volume and the app's folder (the edge then drops its address
and forgets its certificate), then on this server its backups, the jobs' packed sources and every
row of its hosting. A deletion goes on after a restart, and a host an admin turned off counts as
gone. The project itself stays and can go live again from the start.
"""

import asyncio
import logging
import secrets
import shutil
import time

from sqlalchemy import delete, select

from forge_hostworker.wire import HostJob
from forge_web.audit import audit
from forge_web.db.billing_models import AppPayments, RelayCheckout
from forge_web.db.engine import Database
from forge_web.db.hosting_models import (
    AbuseNotice,
    AppBackup,
    AppBackupKey,
    AppDomain,
    AppMail,
    AppMailLog,
    AppOperator,
    AppSecret,
    AppUsageSample,
    Deploy,
    GoLiveApproval,
    HostedApp,
    HostJobRow,
    HostServer,
    Incident,
)
from forge_web.hosting.deploy import ACTIVE
from forge_web.hosting.hosts import HostQueue
from forge_web.settings import WebSettings

log = logging.getLogger(__name__)
ENVIRONMENTS = ("staging", "production")
DELETE_TIMEOUT_S = 600.0
ROWS = (AppUsageSample, AppBackup, AppBackupKey, AppMailLog, AppMail, AppSecret, AppOperator,
        AbuseNotice, GoLiveApproval, Deploy, AppDomain, AppPayments, RelayCheckout)  # fmt: skip


class Removals:
    """Deletes hosted apps completely, and finishes the deletions a restart interrupted."""

    def __init__(self, db: Database, settings: WebSettings, queue: HostQueue):
        self.db, self.queue = db, queue
        self.root = settings.data_dir / "hosting"
        self.tasks: dict[str, asyncio.Task[str]] = {}

    async def start(self) -> None:
        """Go on with the deletions that were under way."""
        async with self.db.session() as session:
            pending = list(await session.scalars(
                select(HostedApp.project_id).where(HostedApp.deleting_at > 0)))  # fmt: skip
        for project_id in pending:
            self.launch(project_id)

    async def close(self) -> None:
        for task in self.tasks.values():
            task.cancel()
        await asyncio.gather(*self.tasks.values(), return_exceptions=True)

    async def request(self, project_id: str) -> None:
        """Mark the app as being deleted (no deploys, backups or alerts from now on), then go."""
        async with self.db.session() as session, session.begin():
            hosted = await session.get(HostedApp, project_id)
            if hosted is None:
                raise LookupError("the app is not hosted")
            busy = await session.scalar(select(Deploy.id).where(
                Deploy.project_id == project_id, Deploy.status.in_(ACTIVE)))  # fmt: skip
            if busy is not None and not hosted.deleting_at:
                raise ValueError("a deploy is under way; delete the app once it ended")
            hosted.deleting_at = hosted.deleting_at or time.time()
            hosted.deleting_error = ""
        self.launch(project_id)

    def launch(self, project_id: str) -> "asyncio.Task[str]":
        """Run the deletion unless it runs already."""
        task = self.tasks.get(project_id)
        if task is None or task.done():
            task = asyncio.create_task(self.delete(project_id))
            self.tasks[project_id] = task
        return task

    async def delete(self, project_id: str) -> str:
        """Delete the app on its host, then here; "" once it is gone, else why not (kept)."""
        problem = await self.on_host(project_id)
        if problem:
            async with self.db.session() as session, session.begin():
                hosted = await session.get(HostedApp, project_id)
                if hosted is not None:
                    hosted.deleting_error = problem[:2000]
            log.warning("hosting: deleting %s: %s", project_id, problem)
            return problem
        app = await self.forget(project_id)
        if app:
            await audit(self.db, "hosting.app_deleted", target=project_id, app=app)
        return ""

    async def on_host(self, project_id: str) -> str:
        """Delete the app on its host, environment by environment ("" when done)."""
        async with self.db.session() as session:
            hosted = await session.get(HostedApp, project_id)
            host = await session.get(HostServer, hosted.host_id) if hosted else None
        if hosted is None or host is None or not host.enabled:
            return ""  # nothing to do there: gone already, or a host an admin turned off
        for environment in ENVIRONMENTS:
            job = HostJob(id=secrets.token_hex(16), kind="delete", app=hosted.app,
                          environment=environment, timeout_s=DELETE_TIMEOUT_S)  # fmt: skip
            await self.queue.send(host.id, "", job)
            result = await self.queue.result(job.id)
            if not result.ok:
                return result.error or "the host could not delete the app"
        return ""

    async def forget(self, project_id: str) -> str:
        """Everything of the app on this server: backups, packed sources, rows; its name."""
        async with self.db.session() as session:
            hosted = await session.get(HostedApp, project_id)
            jobs = list(await session.scalars(select(HostJobRow.id).where(
                HostJobRow.app == hosted.app))) if hosted else []  # fmt: skip
        if hosted is None:
            return ""
        await asyncio.to_thread(shutil.rmtree, self.root / "backups" / project_id, True)
        for job_id in jobs:
            await asyncio.to_thread(shutil.rmtree, self.root / "jobs" / job_id, True)
        async with self.db.session() as session, session.begin():
            await session.execute(delete(HostJobRow).where(HostJobRow.app == hosted.app))
            for model in ROWS:
                await session.execute(delete(model).where(model.project_id == project_id))
            await session.execute(delete(Incident).where(
                Incident.key.like(f"%:{project_id}:%")))  # fmt: skip
            await session.execute(delete(HostedApp).where(HostedApp.project_id == project_id))
        return hosted.app
