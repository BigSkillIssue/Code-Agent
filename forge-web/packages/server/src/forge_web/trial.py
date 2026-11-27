"""The trial (W72): a new account without a plan tries Forge for `billing.trial_days` days with
`billing.trial_credit_usd` on the server's AI keys, both fixed when the account is made.

While it lasts the account has the limits of one without a plan, plus the server's AI keys up to
what is left of the credit (the gateway's own meter counts it) and one hosted app. When the days
or the credit run out, runs are refused ("choose a plan"). One mail comes three days before the
end and one at the end. The hosted apps of a trial that ended without a plan run 14 more days;
then dunning suspends them as for an unpaid bill and takes them on from there (notice, export,
deletion), and the first paid invoice lets them run again. A plan (paid, given by an admin, or
the server's free plan) ends the trial. Accounts from before W72 and admins have none.

Stripe's own trial days of a plan (W40a) are something else: a paid plan that starts with free
days, card first. Both can follow each other: this trial first, then the plan with its days.
"""

import asyncio
import contextlib
import logging
import time
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any

from fastapi import HTTPException
from sqlalchemy import func, select

from forge_web.auth.mail import mail_enabled, send_mail
from forge_web.billing.dunning import Dunning
from forge_web.billing.entitlements import EntitlementEngine, Entitlements
from forge_web.db.billing_models import Trial
from forge_web.db.engine import Database
from forge_web.db.models import Project, UsageRecord, User
from forge_web.gateway.meter import month_start
from forge_web.settings import WebSettings

if TYPE_CHECKING:  # the services hold the trials
    from forge_web.services import Services

log = logging.getLogger(__name__)
DAY = 86400.0
REMIND_DAYS = 3  # the mail before the end
KEEP_APPS_DAYS = 14  # hosted apps run on after the end
TRIAL_APPS = 1  # hosted apps during the trial
TICK_S = 3600.0
OVER = "the trial is over; please choose a plan"


@dataclass(frozen=True)
class TrialState:
    """Where a trial stands: active, expired (its days), used_up (its credit) or paid."""

    started_at: float
    ends_at: float
    credit_usd: float
    used_usd: float
    status: str

    @property
    def over(self) -> bool:
        """Runs are refused."""
        return self.status in ("expired", "used_up")

    def view(self) -> dict[str, Any]:
        """The trial as the web UI sees it."""
        return {"started_at": self.started_at, "ends_at": self.ends_at,
                "credit_usd": self.credit_usd, "used_usd": round(self.used_usd, 4),
                "status": self.status}  # fmt: skip


def status_of(row: Trial, used: float, now: float, paid: bool) -> str:
    """A trial's status from its row, the credit used, the time and whether there is a plan."""
    if paid:
        return "paid"
    if row.ended_reason in ("expired", "used_up"):
        return row.ended_reason  # an ended trial stays ended
    if used >= row.credit_usd:
        return "used_up"
    return "expired" if now >= row.ends_at else "active"


class Trials:
    """Starts, measures and ends the trials of new accounts."""

    def __init__(self, db: Database, settings: WebSettings, engine: EntitlementEngine,
                 dunning: Dunning) -> None:  # fmt: skip
        self.db, self.settings, self.engine, self.dunning = db, settings, engine, dunning
        self.task: asyncio.Task[None] | None = None

    def new(self, user: User, now: float) -> Trial | None:
        """The trial of an account being made now (None for admins or with no trial days)."""
        billing = self.settings.billing
        if user.role == "admin" or billing.trial_days <= 0:
            return None
        return Trial(user_id=user.id, started_at=now, ends_at=now + billing.trial_days * DAY,
                     credit_usd=billing.trial_credit_usd)  # fmt: skip

    async def row(self, user_id: str) -> Trial | None:
        """The user's trial, if they have one."""
        async with self.db.session() as session:
            return await session.get(Trial, user_id)

    async def spent(self, user_id: str, since: float) -> float:
        """What the user's calls on the server's AI keys cost since then (USD)."""
        async with self.db.session() as session:
            total = await session.scalar(
                select(func.coalesce(func.sum(UsageRecord.cost_usd), 0.0)).where(
                    UsageRecord.user_id == user_id, UsageRecord.key_kind == "server",
                    UsageRecord.created_at >= since))  # fmt: skip
        return float(total or 0.0)

    async def state(self, user: User, now: float | None = None) -> TrialState | None:
        """The user's trial now (None: no trial, or billing is off)."""
        row = await self.row(user.id) if self.settings.billing.enabled else None
        if row is None:
            return None
        paid = await self.engine.plan_of(user) is not None
        return await self.measured(row, time.time() if now is None else now, paid)

    async def measured(self, row: Trial, now: float, paid: bool) -> TrialState:
        """The state of a trial whose plan question is answered."""
        used = await self.spent(row.user_id, row.started_at)
        return TrialState(row.started_at, row.ends_at, row.credit_usd, used,
                          status_of(row, used, now, paid))  # fmt: skip

    async def refusal(self, user: User, now: float | None = None) -> str:
        """Why the user's runs are refused ("" when they are not)."""
        state = await self.state(user, now)
        return OVER if state is not None and state.over else ""

    async def limits(self, user: User, unpaid: Entitlements) -> Entitlements:
        """The limits of a user without a plan: during the trial the server's AI keys up to what
        is left of its credit, and a hosted app; otherwise `unpaid` as it is."""
        row = await self.row(user.id)
        if row is None:
            return unpaid
        state = await self.measured(row, time.time(), paid=False)
        if state.status != "active":
            return unpaid
        # The gateway's meter counts by month: what this month has cost plus what is left.
        left = await self.spent(user.id, month_start()) + state.credit_usd - state.used_usd
        policy = unpaid.ai_month_usd if unpaid.server_keys else None
        return replace(unpaid, source="trial", server_keys=True, hosted_apps=TRIAL_APPS,
                       ai_month_usd=left if policy is None else min(left, policy))  # fmt: skip

    async def tick(self, now: float) -> list[tuple[str, str]]:
        """Send the mails and end the trials whose time has come; (user, what happened) each."""
        if not self.settings.billing.enabled:
            return []
        async with self.db.session() as session:
            rows = list(await session.scalars(select(Trial).where(
                Trial.apps_handed_at == 0, Trial.ended_reason != "paid")))  # fmt: skip
        happened = []
        for row in rows:
            what = await self.follow(row, now)
            if what:
                happened.append((row.user_id, what))
        return happened

    async def follow(self, row: Trial, now: float) -> str:
        """Move one trial on: "reminded", "expired", "used_up", "paid", "suspended" or ""."""
        async with self.db.session() as session:
            user = await session.get(User, row.user_id)
        if user is None:
            return ""
        paid = await self.engine.plan_of(user) is not None
        if row.ended_at:  # ended without a plan: its apps run on for a while
            if paid:
                await self.mark(row.user_id, apps_handed_at=now)
                return ""
            if now < row.ended_at + KEEP_APPS_DAYS * DAY:
                return ""
            await self.dunning.trial_over(row.user_id, now)
            await self.mark(row.user_id, apps_handed_at=now)
            return "suspended"
        state = await self.measured(row, now, paid)
        if state.status == "active":
            return await self.remind(row, user, now)
        ended = min(now, row.ends_at) if state.status == "expired" else now
        await self.mark(row.user_id, ended_at=ended, ended_reason=state.status)
        if state.status != "paid":
            await self.mail(user, "Deine Testphase ist vorbei", self.end_text(state.status))
        return state.status

    async def remind(self, row: Trial, user: User, now: float) -> str:
        """The mail three days before the end, once (not for trials that short)."""
        before = row.ends_at - REMIND_DAYS * DAY
        if row.reminded_at or now < before or before <= row.started_at:
            return ""
        await self.mark(row.user_id, reminded_at=now)
        day = time.strftime("%d.%m.%Y", time.gmtime(row.ends_at))
        await self.mail(user, f"Deine Testphase endet in {REMIND_DAYS} Tagen",
                        f"Deine Testphase bei Forge endet am {day}. Damit du danach weiterbauen "
                        f"kannst, wähle einen Tarif: {self.billing_url()}\n\nDeine Projekte "
                        "bleiben in jedem Fall erhalten.")  # fmt: skip
        return "reminded"

    def end_text(self, reason: str) -> str:
        """The mail at the end."""
        why = ("Dein Testguthaben ist aufgebraucht." if reason == "used_up"
               else "Die Tage deiner Testphase sind um.")  # fmt: skip
        return (f"{why} Forge baut weiter, sobald du einen Tarif gewählt hast: "
                f"{self.billing_url()}\n\nDeine Projekte bleiben erhalten. Gehostete Apps laufen "
                f"noch {KEEP_APPS_DAYS} Tage weiter; danach werden sie angehalten.")  # fmt: skip

    def billing_url(self) -> str:
        """Where a plan is chosen."""
        return f"{self.settings.base_url()}/billing"

    async def mark(self, user_id: str, **values: Any) -> None:
        """Store what happened to a trial."""
        async with self.db.session() as session, session.begin():
            row = await session.get(Trial, user_id)
            if row is not None:
                for name, value in values.items():
                    setattr(row, name, value)

    async def mail(self, user: User, subject: str, text: str) -> None:
        """One mail to the account, if mail is set up."""
        smtp = self.settings.auth.smtp
        if user.email and mail_enabled(smtp):
            await send_mail(smtp, user.email, subject, text)

    def start(self) -> None:
        """Follow the trials in the background (hourly)."""
        self.task = asyncio.create_task(self.keep_going())

    async def close(self) -> None:
        """Stop following them."""
        if self.task is not None:
            self.task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.task

    async def keep_going(self) -> None:
        """One round an hour; a bad round is logged, never the end."""
        while True:
            try:
                await self.tick(time.time())
            except Exception:
                log.exception("following the trials failed")
            await asyncio.sleep(TICK_S)


async def check_trial(services: "Services", project_id: str) -> None:
    """402 when the trial of the project's owner is over (runs need a plan then)."""
    async with services.db.session() as session:
        project = await session.get(Project, project_id)
        owner = await session.get(User, project.owner_id) if project else None
    if owner is not None and await services.trials.refusal(owner):
        raise HTTPException(402, OVER)
