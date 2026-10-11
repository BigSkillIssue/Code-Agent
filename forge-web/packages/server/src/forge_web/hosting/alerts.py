"""Metering and alerts of hosted apps (W29b).

Hosts report every few minutes what each app uses (`POST /api/host/metrics`); the samples are
kept for `KEEP_DAYS` and add up per app (CPU hours, traffic, the largest disk use), which is what
billing will charge for. The alert check turns what it sees into incidents: a silent host, an app
that does not run completely, an app over its memory or disk, a host short of disk, a backup that
is overdue or a restore drill that failed. Each incident is mailed to the admins once, when it
begins; it ends by itself when the condition is gone, and a new one is mailed if it comes back.
"""

import asyncio
import contextlib
import json
import logging
import secrets
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from itertools import pairwise
from typing import Any

from sqlalchemy import delete, select

from forge_hostworker.wire import DeployPlan, HostUsage
from forge_web.auth.mail import mail_enabled, send_mail
from forge_web.db.engine import Database
from forge_web.db.hosting_models import (
    AppBackup,
    AppUsageSample,
    Deploy,
    HostedApp,
    HostServer,
    Incident,
)
from forge_web.db.models import User
from forge_web.settings import WebSettings

log = logging.getLogger(__name__)
KEEP_DAYS = 31
CHECK_EVERY_S = 300.0
HOST_SILENT_S = 600.0  # a host that has not asked for work this long is silent
SAMPLE_FRESH_S = 1_200.0  # samples older than this say nothing about now
MEMORY_SHARE = 0.9  # of the app's memory limit
DISK_SPARE_GB = 2.0  # beyond the app's own files: its database and releases
HOST_DISK_LOW_GB = 5.0
Mailer = Callable[[str, str, str], Awaitable[None]]  # to, subject, text


async def store_usage(db: Database, host: HostServer, report: HostUsage) -> int:
    """Keep a host's report for the apps that live on that host; how many samples."""
    now, kept = time.time(), 0
    async with db.session() as session, session.begin():
        stored = await session.get(HostServer, host.id)
        if stored is not None:
            stored.disk_free_gb = report.disk_free_gb
        own = {row.app: row for row in await session.scalars(
            select(HostedApp).where(HostedApp.host_id == host.id))}  # fmt: skip
        for app in report.apps:
            hosted = own.get(app.app)
            if hosted is None:
                continue  # a host speaks only for its own apps
            values = app.model_dump(exclude={"app", "environment"})
            session.add(AppUsageSample(id=secrets.token_hex(8), project_id=hosted.project_id,
                                       environment=app.environment, at=now, **values))  # fmt: skip
            kept += 1
        await session.execute(delete(AppUsageSample).where(
            AppUsageSample.at < now - KEEP_DAYS * 86400))  # fmt: skip
    return kept


def totals(samples: list[AppUsageSample]) -> dict[str, float]:
    """What samples of one app and environment add up to: CPU hours, traffic, peaks."""
    ordered = sorted(samples, key=lambda s: s.at)
    cpu_hours = traffic = 0.0
    for before, after in pairwise(ordered):
        hours = (after.at - before.at) / 3600
        cpu_hours += before.cpu_percent / 100 * hours
        sent = (after.net_rx_mb + after.net_tx_mb) - (before.net_rx_mb + before.net_tx_mb)
        traffic += sent if sent >= 0 else after.net_rx_mb + after.net_tx_mb  # restarted
    return {
        "cpu_hours": round(cpu_hours, 4),
        "traffic_mb": round(traffic, 3),
        "memory_mb_max": max((s.memory_mb for s in ordered), default=0.0),
        "disk_mb_max": max((s.disk_mb for s in ordered), default=0.0),
        "samples": float(len(ordered)),
    }


@dataclass
class Seen:
    """The conditions one check found: incident key -> (subject, detail)."""

    found: dict[str, tuple[str, str]] = field(default_factory=dict)

    def add(self, key: str, subject: str, detail: str = "") -> None:
        self.found[key] = (subject, detail)


class Alerts:
    """Turns what hosting looks like into incidents, and mails each once."""

    def __init__(self, db: Database, settings: WebSettings, mailer: Mailer | None = None):
        self.db, self.settings = db, settings
        self.mailer = mailer or self.smtp_mail
        self.task: asyncio.Task[None] | None = None

    def start(self) -> None:
        """Look now and then, in the background."""
        self.task = asyncio.create_task(self.keep_going())

    async def close(self) -> None:
        if self.task is not None:
            self.task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.task

    async def keep_going(self) -> None:
        while True:
            try:
                await self.check()
            except Exception:  # a bad look must not end the checks
                log.exception("the hosting alerts failed")
            await asyncio.sleep(CHECK_EVERY_S)

    async def smtp_mail(self, to: str, subject: str, text: str) -> None:
        smtp = self.settings.auth.smtp
        if mail_enabled(smtp) and to:
            await send_mail(smtp, to, subject, text)

    async def check(self) -> list[str]:
        """Look once; the keys of the incidents that began now."""
        seen = Seen()
        now = time.time()
        await self.hosts(seen, now)
        await self.apps(seen, now)
        return await self.reconcile(seen, now)

    async def hosts(self, seen: Seen, now: float) -> None:
        async with self.db.session() as session:
            hosts = list(await session.scalars(select(HostServer).where(HostServer.enabled)))
            used = set(await session.scalars(select(HostedApp.host_id)))
        for host in hosts:
            if host.id in used and now - host.last_seen > HOST_SILENT_S:
                seen.add(f"silent:{host.id}", f"App-Server {host.name} meldet sich nicht",
                         "Läuft forge-host-worker? journalctl -u forge-host-worker")  # fmt: skip
            if host.last_seen and 0 < host.disk_free_gb < HOST_DISK_LOW_GB:
                seen.add(f"disk:{host.id}", f"App-Server {host.name}: wenig Platz",
                         f"noch {host.disk_free_gb:.1f} GB frei")  # fmt: skip

    async def apps(self, seen: Seen, now: float) -> None:
        async with self.db.session() as session:
            live = list(await session.execute(
                select(Deploy, HostedApp).join(HostedApp, HostedApp.project_id == Deploy.project_id)
                .where(Deploy.status == "live", HostedApp.suspended.is_(False),
                       HostedApp.deleting_at == 0)))  # fmt: skip
            for deploy, hosted in live:
                sample = await session.scalar(
                    select(AppUsageSample).where(AppUsageSample.project_id == deploy.project_id,
                                                 AppUsageSample.environment == deploy.environment,
                                                 AppUsageSample.at >= now - SAMPLE_FRESH_S)
                    .order_by(AppUsageSample.at.desc()).limit(1))  # fmt: skip
                backup = await session.scalar(
                    select(AppBackup).where(AppBackup.project_id == deploy.project_id,
                                            AppBackup.environment == deploy.environment)
                    .order_by(AppBackup.created_at.desc()).limit(1))  # fmt: skip
                self.app(seen, now, deploy, hosted, sample, backup)

    def app(self, seen: Seen, now: float, deploy: Deploy, hosted: HostedApp,
            sample: AppUsageSample | None, backup: AppBackup | None) -> None:  # fmt: skip
        """What is wrong with one live app and environment."""
        name, where = (
            f"{hosted.app} ({deploy.environment})",
            f"{deploy.project_id}:{deploy.environment}",
        )
        plan = DeployPlan.model_validate(json.loads(deploy.data)["plan"])
        if sample is not None and sample.running < sample.containers:
            seen.add(f"down:{where}", f"{name} läuft nicht vollständig",
                     f"{sample.running} von {sample.containers} Containern laufen")  # fmt: skip
        limit = plan.limits.memory_mb * max(sample.containers if sample else 1, 1)
        if sample is not None and sample.memory_mb > MEMORY_SHARE * limit:
            seen.add(f"memory:{where}", f"{name} braucht fast seinen ganzen Speicher",
                     f"{sample.memory_mb:.0f} von {limit} MB")  # fmt: skip
        disk = (plan.storage_gb + DISK_SPARE_GB) * 1000
        if sample is not None and sample.disk_mb > disk:
            seen.add(f"diskuse:{where}", f"{name} belegt mehr Platz als vorgesehen",
                     f"{sample.disk_mb:.0f} von {disk:.0f} MB")  # fmt: skip
        every = self.settings.hosting.backup_every_h * 3600
        since = backup.created_at if backup else deploy.updated_at
        if now - since > 2 * every:
            seen.add(f"backup:{where}", f"{name}: die Sicherung ist überfällig",
                     "Die letzte Sicherung ist älter als zwei Intervalle.")  # fmt: skip
        if backup is not None and backup.drill_at and not backup.drill_ok:
            seen.add(f"drill:{where}", f"{name}: Probe-Wiederherstellung fehlgeschlagen",
                     backup.drill_detail)  # fmt: skip

    async def reconcile(self, seen: Seen, now: float) -> list[str]:
        """Open what is new, close what is gone; mail the new ones."""
        async with self.db.session() as session, session.begin():
            open_ = {row.key: row for row in await session.scalars(
                select(Incident).where(Incident.closed_at == 0))}  # fmt: skip
            for key, row in open_.items():
                if key not in seen.found:
                    row.closed_at = now
            new = [key for key in seen.found if key not in open_]
            for key in new:
                subject, detail = seen.found[key]
                session.add(Incident(id=secrets.token_hex(8), key=key, subject=subject,
                                     detail=detail, opened_at=now))  # fmt: skip
            admins = list(await session.scalars(select(User.email).where(User.role == "admin")))
        for key in new:
            subject, detail = seen.found[key]
            for email in admins:
                await self.mailer(
                    email or "", f"Forge Hosting: {subject}", f"{subject}\n\n{detail}"
                )
        return new


def incident_view(row: Incident) -> dict[str, Any]:
    """An incident as the panel shows it."""
    return {"id": row.id, "key": row.key, "subject": row.subject, "detail": row.detail,
            "opened_at": row.opened_at, "closed_at": row.closed_at}  # fmt: skip
