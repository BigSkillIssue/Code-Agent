"""Device sessions (W60): the product's app running on a device that Forge's agent works with as a
person would: an Android emulator on the app's host, or a Windows Sandbox on a Windows machine.

A session is packed (the commit, by the project's sandbox), queued, taken by a worker (a host
only takes its own apps' sessions), built and started there; the worker answers action 0 with
the first screen. The agent's actions queue here and the worker takes them one by one, each
answered with the screen afterwards. A session ends when the agent stops it, after
`idle_minutes` without an action, after `session_minutes`, or when its worker gives up. Sessions
live in this server process; a restart ends them (the worker learns it at its next call).
"""

import asyncio
import contextlib
import hashlib
import logging
import secrets
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from sqlalchemy import func, select

from forge_hostworker.device_wire import DeviceAction, DeviceAnswer, DeviceNext, DeviceOffer
from forge_web.db.device_models import DeviceUsage
from forge_web.db.engine import Database
from forge_web.gateway.meter import month_start
from forge_web.hosting.plan import PlanProblem, SandboxCall, pack_commit
from forge_web.settings import WebSettings

log = logging.getLogger(__name__)

Status = Literal["packing", "queued", "starting", "ready", "ended"]
WorkerKind = Literal["host", "win"]
PLATFORM_OF: dict[WorkerKind, str] = {"host": "android", "win": "windows"}
EXPIRE_EVERY_S = 15.0
KEEP_ENDED_S = 3600.0  # an ended session's error stays readable this long


class DeviceRefused(Exception):
    """A worker's call about a session that is not (or no longer) its own."""


@dataclass
class DeviceSession:
    """One device session of a project."""

    id: str
    platform: str
    project_id: str
    user_id: str
    chat_id: str
    commit: str
    app: str = ""  # the app's name on its host (Android)
    package: str = ""  # the Android app's application ID, found in the packed commit
    host_id: str = ""  # the host that runs it (Android); any Windows machine otherwise
    status: Status = "packing"
    worker_id: str = ""
    created: float = field(default_factory=time.time)
    started: float = 0.0  # when a worker took it
    last_used: float = field(default_factory=time.time)
    error: str = ""
    hint: str = ""
    screen: DeviceAnswer | None = None  # the latest answer with a picture
    seq: int = 0
    actions: deque[DeviceAction] = field(default_factory=deque)
    waiting: dict[int, "asyncio.Future[DeviceAnswer]"] = field(default_factory=dict)
    ready: asyncio.Event = field(default_factory=asyncio.Event)  # started, or ended

    def view(self) -> dict[str, Any]:
        """What the agent and the project's people see of it."""
        return {
            "session": self.id,
            "platform": self.platform,
            "status": self.status,
            "commit": self.commit,
            "error": self.error,
            "hint": self.hint,
            "started": self.started,
            "last_used": self.last_used,
        }


class DeviceSessions:
    """Every device session of this server."""

    def __init__(self, db: Database, settings: WebSettings, call: SandboxCall) -> None:
        self.db = db
        self.settings = settings
        self.call = call
        self.root = settings.data_dir / "devices"
        self.sessions: dict[str, DeviceSession] = {}
        self.changed = asyncio.Condition()
        self.tasks: set[asyncio.Task[None]] = set()
        self._expiry: asyncio.Task[None] | None = None

    def start_expiry(self) -> None:
        """End idle and overlong sessions from now on."""
        self._expiry = asyncio.create_task(self._expire_loop())

    async def close(self) -> None:
        """End every session and stop the background work."""
        for session in list(self.sessions.values()):
            await self.end(session, "the server stopped")
        for task in [*self.tasks, *([self._expiry] if self._expiry else [])]:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task

    def source(self, session: DeviceSession) -> Path:
        """Where the session's packed commit waits for its worker."""
        return self.root / f"{session.id}.tar.gz"

    async def notify(self) -> None:
        """Wake the workers' and the agents' long waits."""
        async with self.changed:
            self.changed.notify_all()

    async def wait_changed(self, seconds: float) -> None:
        """Wait for a change, at most `seconds`."""
        async with self.changed:
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self.changed.wait(), max(0.0, seconds))

    # ------------------------------------------------------------------ the agent's side

    def current(self, project_id: str) -> DeviceSession | None:
        """The project's newest session (also an ended one, for its error)."""
        found = [s for s in self.sessions.values() if s.project_id == project_id]
        return max(found, key=lambda s: s.created, default=None)

    def active(self, project_id: str) -> DeviceSession | None:
        """The project's session that is not over."""
        session = self.current(project_id)
        return session if session is not None and session.status != "ended" else None

    def add(self, session: DeviceSession, checks: Any) -> DeviceSession:
        """Pack the session's commit and queue it; `checks(path)` says what is missing in it."""
        self.sessions[session.id] = session
        task = asyncio.create_task(self._pack(session, checks))
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)
        return session

    async def _pack(self, session: DeviceSession, checks: Any) -> None:
        target = self.source(session)
        try:
            max_bytes = self.settings.hosting.max_source_mb * 1024 * 1024
            await pack_commit(self.call, session.project_id, session.commit, target, max_bytes)
            missing = await asyncio.to_thread(checks, target)
        except PlanProblem as err:
            await self.end(session, str(err), err.hint)
            return
        except Exception as err:  # a bug must not leave the session packing forever
            log.exception("packing device session %s failed", session.id)
            await self.end(session, f"the app could not be packed: {err!r}"[:500])
            return
        if missing:
            await self.end(session, *missing)
            return
        if session.status == "packing":
            session.status = "queued"
            await self.notify()

    async def act(self, session: DeviceSession, **fields: Any) -> DeviceAnswer:
        """One action of the agent, and what the device showed afterwards."""
        session.last_used = time.time()
        if session.status != "ready":
            await self.until_ready(session)
        if session.status != "ready":
            return self.not_ready(session)
        session.seq += 1
        action = DeviceAction(seq=session.seq, **fields)
        answer: asyncio.Future[DeviceAnswer] = asyncio.get_running_loop().create_future()
        session.waiting[action.seq] = answer
        session.actions.append(action)
        await self.notify()
        try:
            found = await asyncio.wait_for(answer, self.settings.devices.action_timeout_s)
        except TimeoutError:
            await self.end(session, "the device did not answer in time")
            return DeviceAnswer(
                seq=action.seq,
                ok=False,
                error=session.error,
                hint="start a new session with device_start",
            )
        finally:
            session.waiting.pop(action.seq, None)
        session.last_used = time.time()
        if found.image_b64:
            session.screen = found
        return found

    async def until_ready(self, session: DeviceSession, seconds: float = 100.0) -> None:
        """Wait (within an MCP call's time) for the app to run on the device."""
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(session.ready.wait(), seconds)

    def not_ready(self, session: DeviceSession) -> DeviceAnswer:
        """An answer while the device is not there (yet)."""
        if session.status == "ended":
            return DeviceAnswer(
                seq=0,
                ok=False,
                error=session.error or "the session ended",
                hint=session.hint or "start a new one with device_start",
            )
        doing = {
            "packing": "packing the commit",
            "queued": "waiting for a free device",
            "starting": "building the app and starting the device",
        }[session.status]
        return DeviceAnswer(
            seq=0,
            ok=False,
            error=f"the device is not ready yet: {doing}",
            hint="call device_screenshot again to wait for it",
        )

    async def stop(self, session: DeviceSession) -> None:
        """The agent is done with the device."""
        await self.end(session, "")

    # ------------------------------------------------------------------ the workers' side

    async def claim(self, kind: WorkerKind, worker_id: str, wait_s: float) -> DeviceOffer | None:
        """The oldest queued session this worker may run; waits up to `wait_s`."""
        deadline = time.monotonic() + wait_s
        while True:
            found = self._take(kind, worker_id)
            left = deadline - time.monotonic()
            if found is not None or left <= 0:
                return found
            await self.wait_changed(left)

    def _take(self, kind: WorkerKind, worker_id: str) -> DeviceOffer | None:
        queued = sorted(
            (
                s
                for s in self.sessions.values()
                if s.status == "queued"
                and s.platform == PLATFORM_OF[kind]
                and (kind == "win" or s.host_id == worker_id)
            ),
            key=lambda s: s.created,
        )
        if not queued:
            return None
        session = queued[0]
        session.status, session.worker_id, session.started = "starting", worker_id, time.time()
        devices = self.settings.devices
        return DeviceOffer(
            id=session.id,
            platform=session.platform,
            project=hashlib.sha256(session.project_id.encode()).hexdigest()[:32],
            app=session.app,
            package=session.package,
            build_image=self.settings.hosting.android_image,
            device_image=devices.android_image,
            start_timeout_s=devices.start_timeout_s,
            max_s=devices.session_minutes * 60,
        )

    def of_worker(self, kind: WorkerKind, worker_id: str, session_id: str) -> DeviceSession:
        """The worker's own session with this id (also an ended one: the worker stops it)."""
        session = self.sessions.get(session_id)
        if (
            session is None
            or session.worker_id != worker_id
            or session.platform != PLATFORM_OF[kind]
        ):
            raise DeviceRefused("this is not one of this worker's device sessions")
        return session

    async def next_action(self, session: DeviceSession, wait_s: float) -> DeviceNext:
        """The agent's next action for the worker; waits up to `wait_s`."""
        deadline = time.monotonic() + wait_s
        while True:
            if session.status == "ended":
                return DeviceNext(ended=True)
            if session.actions:
                return DeviceNext(action=session.actions.popleft())
            left = deadline - time.monotonic()
            if left <= 0:
                return DeviceNext()
            await self.wait_changed(left)

    async def answered(self, session: DeviceSession, answer: DeviceAnswer) -> None:
        """The worker's answer: the app started (seq 0), or what an action did."""
        if answer.seq == 0:
            if not answer.ok:
                await self.end(session, answer.error or "the app did not start", answer.hint)
                return
            if session.status == "starting":
                session.status, session.screen = "ready", answer
                session.last_used = time.time()
                session.ready.set()
                self.source(session).unlink(missing_ok=True)
                await self.notify()
            return
        waiting = session.waiting.get(answer.seq)
        if waiting is not None and not waiting.done():
            waiting.set_result(answer)

    # ------------------------------------------------------------------ ending

    async def end(self, session: DeviceSession, error: str, hint: str = "") -> None:
        """End the session once: its time counted, its waiting actions answered."""
        if session.status == "ended":
            return
        was_running = session.started > 0
        session.status, session.error, session.hint = "ended", error, hint
        session.ready.set()
        for seq, waiting in list(session.waiting.items()):
            if not waiting.done():
                waiting.set_result(DeviceAnswer(seq=seq, ok=False, error=error or "it ended"))
        session.actions.clear()
        self.source(session).unlink(missing_ok=True)
        if was_running:
            await self._record(session)
        await self.notify()

    async def _record(self, session: DeviceSession) -> None:
        async with self.db.session() as db_session, db_session.begin():
            db_session.add(
                DeviceUsage(
                    id=secrets.token_hex(16),
                    user_id=session.user_id,
                    project_id=session.project_id,
                    platform=session.platform,
                    started_at=session.started,
                    ended_at=time.time(),
                )
            )

    async def minutes_used(self, user_id: str) -> float:
        """The user's device minutes this month, running sessions included."""
        start = month_start()
        async with self.db.session() as session:
            rows = await session.execute(
                select(func.sum(DeviceUsage.ended_at - DeviceUsage.started_at)).where(
                    DeviceUsage.user_id == user_id, DeviceUsage.started_at >= start
                )
            )
            seconds = float(rows.scalar() or 0)
        now = time.time()
        seconds += sum(
            now - s.started
            for s in self.sessions.values()
            if s.user_id == user_id and s.status != "ended" and s.started
        )
        return seconds / 60

    async def expire(self, now: float | None = None) -> None:
        """End sessions that sat idle, ran too long or never started; forget old ended ones."""
        now = time.time() if now is None else now
        devices = self.settings.devices
        for session in list(self.sessions.values()):
            if session.status == "ended":
                if now - session.last_used > KEEP_ENDED_S:
                    del self.sessions[session.id]
            elif session.status == "ready" and now - session.last_used > devices.idle_minutes * 60:
                await self.end(session, f"no action for {devices.idle_minutes:g} minutes")
            elif session.started and now - session.started > devices.session_minutes * 60:
                await self.end(
                    session,
                    f"a session lasts at most {devices.session_minutes:g} minutes",
                    "start a new one if you need more",
                )
            elif (
                session.status != "ready" and now - session.created > devices.start_timeout_s + 120
            ):
                await self.end(
                    session,
                    "the device did not start in time",
                    "the build or the device's start took too long; try again",
                )

    async def _expire_loop(self) -> None:
        while True:
            await asyncio.sleep(EXPIRE_EVERY_S)
            try:
                await self.expire()
            except Exception:  # the next round tries again
                log.exception("expiring device sessions failed")
