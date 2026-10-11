"""Deploys (W26): an approved commit to staging, then the same commit to production, step by step.

Each deploy keeps its step and what each step left in `deploys`, so after a restart it goes on
where it stopped. A step that waits for a host job remembers the job's id before sending it and
waits for that same job again after a restart, so no step runs twice; above all the migrations
(`migrated` is set the moment they are done). The steps:

    pack      the approved commit packed by the project's sandbox, the plan resolved from it
    check     the host's own checks: build, migrations on a throwaway database, tests
              (production takes them from the staging deploy of the same commit)
    creator   production only: the creator's "Live schalten"
    admin     production only: an admin's approval (not needed for trusted users and admins)
    backup    the app's database dumped on the host (only when a release is live already)
    migrate   the release's migrations on the app's database, once
    release   the secrets sealed to the host's key, then start, health, switch; a release that
              does not get healthy is removed and the previous one keeps running
    done      live at its address
"""

import asyncio
import contextlib
import json
import logging
import secrets
import shutil
import time
from pathlib import Path
from typing import Any

from sqlalchemy import select

from forge_hostworker.wire import (
    PLANNED,
    RESERVED_APPS,
    WITH_SOURCE,
    DeployPlan,
    HostJob,
    JobResult,
    app_host,
    seal,
)
from forge_web.db.engine import Database
from forge_web.db.hosting_models import AppSecret, Deploy, HostedApp, HostServer
from forge_web.db.models import User
from forge_web.hosting.hosts import HostQueue
from forge_web.hosting.plan import PlanProblem, SandboxCall, make_plan, pack_commit, read_product
from forge_web.settings import WebSettings
from forge_web.vault import Vault

log = logging.getLogger(__name__)
STEPS = ("pack", "check", "creator", "admin", "backup", "migrate", "release", "done")
STAGING_STEPS = tuple(step for step in STEPS if step not in ("creator", "admin"))
SOURCE = "source.tar.gz"
ACTIVE = ("running", "waiting")


class DeployProblem(Exception):
    """A step cannot go on; with a hint for the user."""

    def __init__(self, message: str, hint: str = "") -> None:
        super().__init__(message)
        self.hint = hint


def steps_of(environment: str) -> tuple[str, ...]:
    """The steps of a deploy to this environment."""
    return STEPS if environment == "production" else STAGING_STEPS


def view(row: Deploy) -> dict[str, Any]:
    """A deploy as the pages show it."""
    data = json.loads(row.data or "{}")
    return {"id": row.id, "environment": row.environment, "commit": row.commit,
            "release": row.release, "app": row.app, "step": row.step, "status": row.status,
            "error": row.error, "hint": row.hint, "steps": list(steps_of(row.environment)),
            "checks": data.get("checks", []), "checks_from": data.get("checks_from", ""),
            "url": data.get("url", ""), "log_tail": data.get("log_tail", ""),
            "creator_approved_at": row.creator_approved_at, "admin": data.get("admin", ""),
            "admin_approved_at": row.admin_approved_at, "migrated": row.migrated,
            "created_at": row.created_at, "updated_at": row.updated_at}  # fmt: skip


class Deploys:
    """Runs the deploys, one task each."""

    def __init__(
        self, db: Database, vault: Vault, settings: WebSettings, queue: HostQueue, call: SandboxCall
    ) -> None:
        self.db, self.vault, self.settings, self.queue, self.call = db, vault, settings, queue, call
        self.root = settings.data_dir / "hosting" / "deploys"
        self.tasks: dict[str, asyncio.Task[None]] = {}
        self.packing: dict[str, asyncio.Lock] = {}  # the sandbox packs into one file at a time

    async def start(self) -> None:
        """Go on with the deploys a restart interrupted."""
        async with self.db.session() as session:
            ids = list(await session.scalars(select(Deploy.id).where(Deploy.status == "running")))
        for deploy_id in ids:
            self.launch(deploy_id)

    async def close(self) -> None:
        """Stop the tasks; their deploys stay "running" and go on after the next start."""
        tasks = list(self.tasks.values())
        for task in tasks:
            task.cancel()
        for task in tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task

    def launch(self, deploy_id: str) -> None:
        """Run (or go on with) a deploy in the background."""
        if deploy_id in self.tasks:
            return
        task = asyncio.create_task(self.run(deploy_id))
        self.tasks[deploy_id] = task
        task.add_done_callback(lambda _: self.tasks.pop(deploy_id, None))

    async def run(self, deploy_id: str) -> None:
        """Step after step until done, failed or waiting for an approval."""
        try:
            row = await self.load(deploy_id)
            if row is not None and row.status == "running":
                await self.steps(row)
        except (DeployProblem, PlanProblem) as err:
            await self.failed(deploy_id, str(err), err.hint)
        except asyncio.CancelledError:
            raise
        except Exception as err:  # a bug: the deploy must still not stay "running" forever
            log.exception("deploy %s failed", deploy_id)
            await self.failed(deploy_id, f"the deploy stopped: {err!r}"[:2000])

    async def steps(self, row: Deploy) -> None:
        data: dict[str, Any] = json.loads(row.data or "{}")
        order = steps_of(row.environment)
        step = row.step
        while step != "done":
            if not await getattr(self, f"step_{step}")(row, data):
                await self.save(row, data, step, status="waiting")
                return
            step = order[order.index(step) + 1]
            await self.save(row, data, step, status="live" if step == "done" else "running")
        await self.went_live(row)

    async def load(self, deploy_id: str) -> Deploy | None:
        async with self.db.session() as session:
            return await session.get(Deploy, deploy_id)

    async def save(
        self, row: Deploy, data: dict[str, Any], step: str | None = None, status: str | None = None
    ) -> None:
        """Keep the deploy's place and what its steps left."""
        async with self.db.session() as session, session.begin():
            stored = await session.get(Deploy, row.id)
            if stored is None:
                return
            stored.data, stored.app, stored.migrated = json.dumps(data), row.app, row.migrated
            stored.step, stored.status = step or stored.step, status or stored.status
            stored.updated_at = time.time()
            if status in ("running", "live"):
                stored.error = stored.hint = ""
        row.step, row.status = step or row.step, status or row.status

    async def failed(self, deploy_id: str, error: str, hint: str = "") -> None:
        async with self.db.session() as session, session.begin():
            row = await session.get(Deploy, deploy_id)
            if row is not None:
                row.status, row.error, row.hint = "failed", error[:4000], hint[:1000]
                row.updated_at = time.time()

    async def went_live(self, row: Deploy) -> None:
        """The deploy before it in the same environment is replaced."""
        async with self.db.session() as session, session.begin():
            older = await session.scalars(
                select(Deploy).where(Deploy.project_id == row.project_id,
                                     Deploy.environment == row.environment,
                                     Deploy.status == "live", Deploy.id != row.id)
            )  # fmt: skip
            for other in older:
                other.status, other.updated_at = "replaced", time.time()
        await asyncio.to_thread(shutil.rmtree, self.root / row.id, True)

    # the steps (each returns False to wait for a person) ------------------------------------

    async def step_pack(self, row: Deploy, data: dict[str, Any]) -> bool:
        """The approved commit, its manifest, the app's name on the hosts and the plan."""
        source = await self.packed(row)
        product = await asyncio.to_thread(read_product, source)
        row.app = await self.app_name(row, product.manifest.name)
        hosting = self.settings.hosting
        plan = make_plan(product, row.app, row.environment, row.release, hosting.images,
                         hosting.apps_domain)  # fmt: skip
        missing = await self.missing_secrets(row, plan)
        if missing:
            raise DeployProblem(f"secrets without a value for {row.environment}: "
                                f"{', '.join(missing)}", "set them on the deploy page")  # fmt: skip
        data["plan"] = plan.model_dump(mode="json")
        return True

    async def step_check(self, row: Deploy, data: dict[str, Any]) -> bool:
        """The host's own checks, or those of the staging deploy of this commit."""
        if row.environment == "production" and not data.get("checks"):
            staged = await self.staged(row)
            if staged is not None:
                data["checks"], data["checks_from"] = json.loads(staged.data)["checks"], staged.id
        if data.get("checks"):
            return True
        result = await self.job(row, data, "check", self.settings.hosting.check_timeout_s)
        data["checks"] = [check.model_dump() for check in result.checks]
        data["log_tail"] = result.log_tail[-8000:]
        failing = [check.name for check in result.checks if not check.ok]
        if not result.ok or failing:
            data["checks"] = data["checks"] or [{"name": "build", "ok": False,
                                                  "detail": result.error}]  # fmt: skip
            await self.save(row, data)
            raise DeployProblem(
                f"the host's checks failed: {', '.join(failing) or result.error}",
                result.hint or "the log is on the deploy page; send the product back to Forge",
            )
        return True

    async def step_creator(self, row: Deploy, data: dict[str, Any]) -> bool:
        """Production waits for the creator's "Live schalten"."""
        return (await self.load_approval(row.id))[0] > 0

    async def step_admin(self, row: Deploy, data: dict[str, Any]) -> bool:
        """Then for an admin, unless the creator is trusted or an admin."""
        if (await self.load_approval(row.id))[1] > 0:
            data.setdefault("admin", "approved")
            return True
        async with self.db.session() as session:
            creator = await session.get(User, row.user_id)
        if creator is not None and (creator.hosting_trusted or creator.role == "admin"):
            data["admin"] = "trusted" if creator.role != "admin" else "admin"
            return True
        return False

    async def step_backup(self, row: Deploy, data: dict[str, Any]) -> bool:
        """The app's database dumped on the host before anything changes it."""
        if not self.plan_of(data).database or await self.live_release(row) is None:
            return True
        result = await self.job(row, data, "backup", self.settings.hosting.job_timeout_s)
        if not result.ok:
            hint = result.hint or "nothing was changed; start the deploy again"
            raise DeployProblem(f"the database could not be backed up: {result.error}", hint)
        return True

    async def step_migrate(self, row: Deploy, data: dict[str, Any]) -> bool:
        """The release's migrations, once."""
        plan = self.plan_of(data)
        if row.migrated or not plan.database or not any(s.migrate for s in plan.services):
            return True
        result = await self.job(row, data, "migrate", self.settings.hosting.job_timeout_s)
        if not result.ok:
            data["log_tail"] = result.log_tail[-8000:]
            await self.save(row, data)
            raise DeployProblem(f"the migrations failed: {result.error}",
                                result.hint or "the release before keeps running")  # fmt: skip
        row.migrated = True
        await self.save(row, data)
        return True

    async def step_release(self, row: Deploy, data: dict[str, Any]) -> bool:
        """Start, check health and switch; the previous release stays when it does not work."""
        result = await self.job(row, data, "release", self.settings.hosting.job_timeout_s)
        data["log_tail"] = result.log_tail[-8000:]
        if not result.ok:
            await self.save(row, data)
            if result.rolled_back:
                raise DeployProblem(
                    f"the release did not get healthy and was rolled back: {result.error}",
                    "the release before keeps running; the log is on the deploy page",
                )
            raise DeployProblem(f"the release failed: {result.error}", result.hint)
        data["services"] = [service.model_dump() for service in result.services]
        domain = self.settings.hosting.apps_domain
        data["url"] = f"https://{app_host(row.app, row.environment, domain)}" if domain else ""
        return True

    # helpers --------------------------------------------------------------------------------

    def plan_of(self, data: dict[str, Any]) -> DeployPlan:
        return DeployPlan.model_validate(data["plan"])

    async def packed(self, row: Deploy) -> Path:
        """The deploy's packed commit (packed again when a restart lost it)."""
        target = self.root / row.id / SOURCE
        if not target.is_file():
            max_bytes = self.settings.hosting.max_source_mb * 1024 * 1024
            async with self.packing.setdefault(row.project_id, asyncio.Lock()):
                await pack_commit(self.call, row.project_id, row.commit, target, max_bytes)
        return target

    async def job(self, row: Deploy, data: dict[str, Any], kind: str, timeout: float) -> JobResult:
        """Run one host job of this deploy: the same job again after a restart, never a second."""
        key = f"{kind}_job"
        if not data.get(key):
            data[key] = secrets.token_hex(16)
            await self.save(row, data)
        if not await self.queue.exists(data[key]):
            plan = self.plan_of(data) if kind in PLANNED else None
            job = HostJob(id=data[key], kind=kind, app=row.app, environment=row.environment,
                          plan=plan, timeout_s=timeout)  # fmt: skip
            source = await self.packed(row) if kind in WITH_SOURCE else None
            sealed = await self.sealed(row, self.plan_of(data)) if kind == "release" else None
            await self.queue.send(row.host_id, row.id, job, source, sealed)
        return await self.queue.result(data[key])

    async def sealed(self, row: Deploy, plan: DeployPlan) -> Any:
        """The release's secrets, sealed to its host's key."""
        names = {name for service in plan.services for name in service.secrets}
        values = await self.secret_values(row.project_id, row.environment)
        missing = sorted(names - set(values))
        if missing:
            hint = "set them on the deploy page, then start the deploy again"
            raise DeployProblem(f"secrets without a value: {', '.join(missing)}", hint)
        async with self.db.session() as session:
            host = await session.get(HostServer, row.host_id)
        if host is None:
            raise DeployProblem("the app's host is gone", "an admin adds it again")
        chosen = {name: values[name] for name in sorted(names)}
        return seal(host.public_key, json.dumps(chosen).encode())

    async def secret_values(self, project_id: str, environment: str) -> dict[str, str]:
        async with self.db.session() as session:
            rows = await session.scalars(
                select(AppSecret).where(
                    AppSecret.project_id == project_id, AppSecret.environment == environment
                )
            )
            return {row.name: self.vault.decrypt(row.value) for row in rows}

    async def missing_secrets(self, row: Deploy, plan: DeployPlan) -> list[str]:
        names = {name for service in plan.services for name in service.secrets}
        return sorted(names - set(await self.secret_values(row.project_id, row.environment)))

    async def app_name(self, row: Deploy, wanted: str) -> str:
        """The project's name on the hosts: made once from its manifest, then kept."""
        async with self.db.session() as session, session.begin():
            hosted = await session.get(HostedApp, row.project_id)
            if hosted is not None:
                return hosted.app
            taken = set(await session.scalars(select(HostedApp.app)))
            name = next(candidate for n in range(1, 1000)
                        if (candidate := wanted if n == 1 else f"{wanted[:36]}-{n}")
                        not in taken and candidate not in RESERVED_APPS)  # fmt: skip
            session.add(HostedApp(project_id=row.project_id, app=name, host_id=row.host_id,
                                  created_at=time.time()))  # fmt: skip
        return name

    async def load_approval(self, deploy_id: str) -> tuple[float, float]:
        """When the creator and an admin approved the deploy (0: not yet)."""
        row = await self.load(deploy_id)
        return (row.creator_approved_at, row.admin_approved_at) if row else (0.0, 0.0)

    async def staged(self, row: Deploy) -> Deploy | None:
        """The staging deploy of the same commit that went live with every check passed."""
        async with self.db.session() as session:
            rows = await session.scalars(
                select(Deploy).where(Deploy.project_id == row.project_id,
                                     Deploy.environment == "staging", Deploy.commit == row.commit,
                                     Deploy.status.in_(("live", "replaced")))
                .order_by(Deploy.created_at.desc())
            )  # fmt: skip
            for staged in rows:
                checks = json.loads(staged.data or "{}").get("checks", [])
                if checks and all(check.get("ok") for check in checks):
                    return staged
        return None

    async def live_release(self, row: Deploy) -> Deploy | None:
        """The deploy live in the same environment now, if any."""
        async with self.db.session() as session:
            return await session.scalar(
                select(Deploy).where(Deploy.project_id == row.project_id,
                                     Deploy.environment == row.environment,
                                     Deploy.status == "live", Deploy.id != row.id)
            )  # fmt: skip
