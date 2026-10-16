"""Suspending a hosted app, or letting it run again: the flag, and a stop or start job where it
is live (the edge then drops it or serves it again). Admins do it (`admin.py`), upheld notices
(`abuse.py`) and unpaid bills (`billing/dunning.py`)."""

import secrets
import time

from sqlalchemy import select

from forge_hostworker.wire import HostJob
from forge_web.db.engine import Database
from forge_web.db.hosting_models import Deploy, HostedApp
from forge_web.hosting.hosts import HostQueue

JOB_TIMEOUT_S = 300.0


async def live_in(db: Database, project_id: str) -> list[str]:
    """The environments in which the project has a live release."""
    async with db.session() as session:
        found = await session.scalars(select(Deploy.environment).where(
            Deploy.project_id == project_id, Deploy.status == "live"))  # fmt: skip
        return sorted(set(found))


async def set_suspended(db: Database, queue: HostQueue, project_id: str, reason: str,
                        suspended: bool) -> list[str] | None:  # fmt: skip
    """Suspend the app or let it run again; the environments it was stopped or started in
    (None when there is no such app)."""
    async with db.session() as session, session.begin():
        hosted = await session.get(HostedApp, project_id)
        if hosted is None:
            return None
        hosted.suspended, hosted.suspended_at = suspended, time.time()
        hosted.suspended_reason = reason.strip() if suspended else ""
    environments = await live_in(db, project_id)
    for environment in environments:
        job = HostJob(id=secrets.token_hex(16), kind="stop" if suspended else "start",
                      app=hosted.app, environment=environment, timeout_s=JOB_TIMEOUT_S)  # fmt: skip
        await queue.send(hosted.host_id, "", job)
    return environments
