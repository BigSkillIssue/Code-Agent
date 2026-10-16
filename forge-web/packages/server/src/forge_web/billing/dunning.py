"""Dunning (W33b2): what happens when a customer does not pay.

A failed payment (Stripe's `invoice.payment_failed`) opens a case and mails the customer; Stripe
keeps trying to collect. After `grace_days` the customer's hosted apps are suspended. After
`notice_after_days` more, every live environment is exported (a fresh backup, which the owner can
download) and the customer is told when the apps will be deleted. After `delete_after_days` more,
and only once each export is there, the apps are deleted. A paid invoice (`invoice.paid`) closes
the case at any stage before that and lets suspended apps run again. Projects and accounts are not
touched: dunning is about what costs the admin money to keep running.
"""

import asyncio
import contextlib
import logging
import secrets
import time
from typing import Any

from sqlalchemy import select

from forge_web.auth.mail import mail_enabled, send_mail
from forge_web.billing.events import Events
from forge_web.db.billing_models import BillingCustomer, DunningCase
from forge_web.db.engine import Database
from forge_web.db.hosting_models import AppBackup, HostedApp
from forge_web.db.models import Project, User
from forge_web.hosting.hosts import HostQueue
from forge_web.hosting.ops import Backups, said
from forge_web.hosting.removal import Removals
from forge_web.hosting.suspension import live_in, set_suspended
from forge_web.settings import WebSettings

log = logging.getLogger(__name__)
DAY = 86400.0
TICK_S = 3600.0
UNPAID = "Die Rechnung ist nicht bezahlt."  # the suspension's reason; only these are lifted


class Dunning:
    """Moves unpaid cases on, each stage at its date."""

    def __init__(self, db: Database, settings: WebSettings, queue: HostQueue, backups: Backups,
                 removals: Removals) -> None:  # fmt: skip
        self.db, self.settings, self.queue = db, settings, queue
        self.backups, self.removals = backups, removals
        self.exports: set[asyncio.Task[None]] = set()
        self.task: asyncio.Task[None] | None = None

    def register(self, events: Events) -> None:
        events.on("invoice.payment_failed", self.failed)
        events.on("invoice.paid", self.paid)

    def start(self) -> None:
        self.task = asyncio.create_task(self.keep_going())

    async def close(self) -> None:
        for task in [self.task, *self.exports]:
            if task is not None:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task

    async def keep_going(self) -> None:
        while True:
            try:
                await self.tick(time.time())
            except Exception:  # a bad round must not end dunning
                log.exception("dunning failed")
            await asyncio.sleep(TICK_S)

    async def failed(self, event: dict[str, Any]) -> None:
        user_id = await self.customer(event)
        if user_id:
            await self.open(user_id, time.time())

    async def paid(self, event: dict[str, Any]) -> None:
        user_id = await self.customer(event)
        if user_id:
            await self.close_case(user_id, time.time())

    async def customer(self, event: dict[str, Any]) -> str | None:
        """The user an invoice event is about."""
        stripe_customer = str(event["data"]["object"].get("customer", ""))
        async with self.db.session() as session:
            return await session.scalar(select(BillingCustomer.user_id).where(
                BillingCustomer.stripe_customer == stripe_customer))  # fmt: skip

    async def open(self, user_id: str, now: float) -> DunningCase:
        """Open a case for the user (once), and tell them."""
        async with self.db.session() as session, session.begin():
            case = await session.scalar(select(DunningCase).where(
                DunningCase.user_id == user_id, DunningCase.closed_at == 0))  # fmt: skip
            if case is not None:
                return case
            case = DunningCase(id=secrets.token_hex(8), user_id=user_id, opened_at=now,
                               stage="grace", stage_at=now)  # fmt: skip
            session.add(case)
        grace = self.settings.billing.grace_days
        await self.mail(user_id, "Deine Zahlung ist fehlgeschlagen",
                        "Wir konnten deine letzte Rechnung nicht einziehen. Bitte aktualisiere "
                        "deine Zahlungsart unter Tarif und Abrechnung. Ohne Zahlung werden deine "
                        f"gehosteten Apps in {grace:.0f} Tagen angehalten.")  # fmt: skip
        return case

    async def close_case(self, user_id: str, now: float) -> None:
        """The bill is paid: end the case, let apps run again that dunning suspended."""
        async with self.db.session() as session, session.begin():
            case = await session.scalar(select(DunningCase).where(
                DunningCase.user_id == user_id, DunningCase.closed_at == 0))  # fmt: skip
            if case is None or case.stage == "deleted":
                return
            case.closed_at = now
        for hosted in await self.apps_of(user_id):
            if hosted.suspended and hosted.suspended_reason == UNPAID:
                await set_suspended(self.db, self.queue, hosted.project_id, "", False)

    async def tick(self, now: float) -> list[tuple[str, str]]:
        """Move every open case on whose date has come; (case, new stage) for each."""
        async with self.db.session() as session:
            cases = list(await session.scalars(select(DunningCase).where(
                DunningCase.closed_at == 0)))  # fmt: skip
        moved = []
        for case in cases:
            stage = await self.next_stage(case, now)
            if stage:
                async with self.db.session() as session, session.begin():
                    row = await session.get(DunningCase, case.id)
                    if row is not None:
                        row.stage, row.stage_at = stage, now
                        row.closed_at = now if stage == "deleted" else 0.0
                moved.append((case.id, stage))
        return moved

    async def next_stage(self, case: DunningCase, now: float) -> str:
        """Do what the case's date calls for; its new stage ("" while it waits)."""
        billing, waited = self.settings.billing, now - case.stage_at
        if case.stage == "grace" and waited >= billing.grace_days * DAY:
            await self.suspend(case.user_id)
            return "suspended"
        if case.stage == "suspended" and waited >= billing.notice_after_days * DAY:
            await self.notice(case.user_id, now)
            return "noticed"
        if case.stage == "noticed" and waited >= billing.delete_after_days * DAY:
            if not await self.exported(case.user_id, case.stage_at):
                return ""  # nothing is deleted before its export is there
            for hosted in await self.apps_of(case.user_id):
                await self.removals.request(hosted.project_id)
            await self.mail(case.user_id, "Deine Apps wurden gelöscht",
                            "Weil die Rechnung offen blieb, haben wir deine gehosteten Apps "
                            "gelöscht. Dein Konto und deine Projekte bleiben.")  # fmt: skip
            return "deleted"
        return ""

    async def suspend(self, user_id: str) -> None:
        for hosted in await self.apps_of(user_id):
            await set_suspended(self.db, self.queue, hosted.project_id, UNPAID, True)
        await self.mail(user_id, "Deine Apps sind angehalten",
                        "Deine Rechnung ist weiter offen, deshalb sind deine gehosteten Apps "
                        "angehalten. Sobald sie bezahlt ist, laufen sie wieder.")  # fmt: skip

    async def notice(self, user_id: str, now: float) -> None:
        """Export every live environment and announce the deletion."""
        for hosted in await self.apps_of(user_id):
            for environment in await live_in(self.db, hosted.project_id):
                task = asyncio.create_task(said(self.backups.backup(hosted.project_id,
                                                                    environment)))  # fmt: skip
                self.exports.add(task)
                task.add_done_callback(self.exports.discard)
        day = time.strftime("%d.%m.%Y", time.gmtime(now + self.settings.billing.delete_after_days
                                                    * DAY))  # fmt: skip
        await self.mail(user_id, "Deine Apps werden gelöscht",
                        f"Deine Rechnung ist weiter offen. Am {day} löschen wir deine gehosteten "
                        "Apps. Ihre Daten kannst du vorher unter Live schalten herunterladen; "
                        "wir haben dafür gerade einen Export angelegt. Wenn du bezahlst, bleibt "
                        "alles, wie es ist.")  # fmt: skip

    async def exported(self, user_id: str, since: float) -> bool:
        """Whether each live environment of the user's apps has a backup made since `since`."""
        for hosted in await self.apps_of(user_id):
            for environment in await live_in(self.db, hosted.project_id):
                async with self.db.session() as session:
                    found = await session.scalar(select(AppBackup.id).where(
                        AppBackup.project_id == hosted.project_id,
                        AppBackup.environment == environment,
                        AppBackup.created_at >= since))  # fmt: skip
                if found is None:
                    return False
        return True

    async def apps_of(self, user_id: str) -> list[HostedApp]:
        """The hosted apps of the user's projects (not those being deleted)."""
        async with self.db.session() as session:
            return list(await session.scalars(
                select(HostedApp).join(Project, Project.id == HostedApp.project_id)
                .where(Project.owner_id == user_id, HostedApp.deleting_at == 0)))  # fmt: skip

    async def mail(self, user_id: str, subject: str, text: str) -> None:
        smtp = self.settings.auth.smtp
        async with self.db.session() as session:
            user = await session.get(User, user_id)
        if user is not None and user.email and mail_enabled(smtp):
            await send_mail(smtp, user.email, subject, text)
