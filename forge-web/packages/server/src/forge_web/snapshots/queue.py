"""A snapshot after every run (W66): when a run ends the run manager asks here, and runs of a
project that end within `debounce_s` make one snapshot. Nothing here makes a run wait or fail:
the work happens in a task of its own, and what goes wrong is logged and recorded.

The sandbox records the working tree and packs a bundle (incremental, a full one every
`full_every` and after a restore or a failure); the bundle is read over in parts with a size
limit, checked to be a bundle, sealed and kept (`store.py`). A tick now and then applies
retention and moves snapshots kept here to a storage node once there is one.
"""

import asyncio
import base64
import contextlib
import logging
import re
import secrets
import tempfile
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ValidationError
from sqlalchemy import delete, select

from forge_sandbox.methods import SNAPSHOT_REF
from forge_sandbox.mux import ChannelClosed
from forge_sandbox.rpc import RpcError
from forge_web.containers.driver import SandboxError
from forge_web.db.engine import Database
from forge_web.db.snapshot_models import ProjectSnapshot
from forge_web.settings import WebSettings
from forge_web.snapshots.store import KEPT, SnapshotProblem, SnapshotStore, kept

log = logging.getLogger(__name__)
SandboxCall = Callable[[str, str, dict[str, Any]], Awaitable[Any]]
SANDBOX_ERRORS = (RpcError, ChannelClosed, SandboxError, OSError, TimeoutError)
BUNDLE = ".git/forge-transfer/snapshot.bundle"  # where git.snapshot leaves its bundle
BUNDLE_HEADS = (b"# v2 git bundle\n", b"# v3 git bundle\n")
PART = 512 * 1024
TICK_S = 600.0
MOVES_PER_TICK = 20
HEX = r"^[0-9a-f]{40,64}$"


class Made(BaseModel):
    """What git.snapshot answers for a new snapshot (the sandbox is not trusted)."""

    ref: str
    commit: str
    tree: str
    full: bool
    size: int

    def check(self, max_bytes: int) -> None:
        """Refuse what does not look right."""
        if not re.match(SNAPSHOT_REF, self.ref) or not re.match(HEX, self.commit):
            raise SnapshotProblem("the sandbox answered a snapshot that is not one")
        if not re.match(HEX, self.tree) or not self.size > 0:
            raise SnapshotProblem("the sandbox answered a snapshot that is not one")
        if self.size > max_bytes:
            raise SnapshotProblem(f"the snapshot is larger than {max_bytes // 2**20} MB")


async def read_bundle(call: SandboxCall, project_id: str, target: Path, max_bytes: int) -> int:
    """The sandbox's bundle copied into `target` (at most `max_bytes`, and a git bundle)."""
    size = 0
    with target.open("wb") as out:
        while True:
            part = await call(project_id, "fs.read", {"path": BUNDLE, "offset": size,
                                                      "limit": PART})  # fmt: skip
            chunk = part_bytes(part)
            size += len(chunk)
            if size > max_bytes:
                raise SnapshotProblem(f"the snapshot is larger than {max_bytes // 2**20} MB")
            out.write(chunk)
            if not isinstance(part, dict) or not part.get("truncated") or not chunk:
                break
    with target.open("rb") as written:
        if written.read(16) not in BUNDLE_HEADS:
            raise SnapshotProblem("what the sandbox packed is not a git bundle")
    return size


def part_bytes(part: Any) -> bytes:
    """The bytes of one read result (text or base64)."""
    if not isinstance(part, dict):
        raise SnapshotProblem("the sandbox sent an unexpected answer")
    if isinstance(part.get("base64"), str):
        return base64.b64decode(part["base64"])
    return str(part.get("text") or "").encode("utf-8")


class SnapshotQueue:
    """Joins the ends of runs per project and makes their snapshots on the side."""

    def __init__(
        self,
        db: Database,
        settings: WebSettings,
        store: SnapshotStore,
        call: SandboxCall,
        running: Callable[[str], bool],
    ) -> None:
        self.db, self.settings, self.store = db, settings, store
        self.call, self.running = call, running  # running: the project's sandbox is connected
        self.wanted: dict[str, str] = {}  # project id -> the chat whose run ended last
        self.tasks: dict[str, asyncio.Task[None]] = {}
        self.restored: set[str] = set()  # projects whose next snapshot is a full one
        self.ticker: asyncio.Task[None] | None = None

    def request(self, project_id: str, chat_id: str) -> None:
        """A run of the project ended: a snapshot follows once the project is quiet."""
        if not self.settings.snapshots.enabled:
            return
        self.wanted[project_id] = chat_id
        if project_id not in self.tasks:
            self.tasks[project_id] = asyncio.create_task(self._later(project_id))

    async def _later(self, project_id: str) -> None:
        try:
            while project_id in self.wanted:
                await asyncio.sleep(self.settings.snapshots.debounce_s)
                chat_id = self.wanted.pop(project_id, "")
                with contextlib.suppress(Exception):  # logged inside; never ends the queue
                    await self.snapshot(project_id, chat_id)
        finally:  # no await after the last check: a request now starts a new task
            self.tasks.pop(project_id, None)

    async def snapshot(self, project_id: str, chat_id: str = "") -> ProjectSnapshot | None:
        """Make one snapshot now; None when the tree is unchanged or the sandbox is not
        running. A failure is logged and recorded (the next snapshot is a full one)."""
        if not self.running(project_id):
            return None
        try:
            return await self._make(project_id, chat_id)
        except RpcError as err:
            if err.code == "no_repo":
                return None
            problem = f"the sandbox could not make the snapshot: {err.message}"
        except (SnapshotProblem, ValidationError) as err:
            problem = str(err)
        except SANDBOX_ERRORS as err:
            problem = f"the sandbox was not reachable: {err}"
        except Exception as err:
            log.exception("snapshot of project %s failed", project_id)
            problem = f"the snapshot failed: {err}"
        log.warning("snapshot of project %s: %s", project_id, problem)
        await self._record_failure(project_id, chat_id, problem)
        return None

    async def _make(self, project_id: str, chat_id: str) -> ProjectSnapshot | None:
        previous, full = await self._plan(project_id)
        params = {"previous": previous.ref if previous else None, "full": full}
        answer = await self.call(project_id, "git.snapshot", params)
        if not isinstance(answer, dict):
            raise SnapshotProblem("the sandbox sent an unexpected answer")
        if answer.get("unchanged") is True:
            return None
        max_bytes = self.settings.snapshots.max_mb * 1024 * 1024
        made = Made.model_validate(answer)
        snapshot_id = secrets.token_hex(16)
        with tempfile.TemporaryDirectory(prefix="forge-snapshot-") as work:
            plain = Path(work) / "snapshot.bundle"
            try:
                made.check(max_bytes)
                await read_bundle(self.call, project_id, plain, max_bytes)
            finally:
                with contextlib.suppress(*SANDBOX_ERRORS):
                    await self.call(project_id, "fs.delete", {"path": BUNDLE})
            size, sha256 = await self.store.seal(project_id, snapshot_id, plain)
        incremental = previous is not None and not full and not made.full
        row = ProjectSnapshot(
            id=snapshot_id, project_id=project_id, chat_id=chat_id[:32], ref=made.ref,
            commit=made.commit, tree=made.tree, kind="incr" if incremental else "full",
            parent_id=previous.id if incremental and previous else None, size=size,
            sha256=sha256, state="stored", created_at=time.time(),
        )  # fmt: skip
        async with self.db.session() as session, session.begin():
            session.add(row)
        self.restored.discard(project_id)
        await self.store.hand_over(snapshot_id)
        return row

    async def _plan(self, project_id: str) -> tuple[ProjectSnapshot | None, bool]:
        """The snapshot the next builds on, and whether the next is a full one."""
        every = self.settings.snapshots.full_every
        async with self.db.session() as session:
            rows = list(await session.scalars(
                select(ProjectSnapshot).where(ProjectSnapshot.project_id == project_id,
                                              ProjectSnapshot.state != "deleted")
                .order_by(ProjectSnapshot.created_at.desc()).limit(every + 1)))  # fmt: skip
        good = [row for row in rows if row.state in KEPT]
        previous = good[0] if good else None
        since_full = 0
        for row in good:
            if row.kind == "full":
                break
            since_full += 1
        failed = bool(rows) and rows[0].state == "failed"
        full = previous is None or failed or project_id in self.restored or since_full >= every
        return previous, full

    async def _record_failure(self, project_id: str, chat_id: str, problem: str) -> None:
        with contextlib.suppress(Exception):  # the database may be what failed
            async with self.db.session() as session, session.begin():
                session.add(ProjectSnapshot(
                    id=secrets.token_hex(16), project_id=project_id, chat_id=chat_id[:32],
                    state="failed", error=problem[:2000], created_at=time.time()))  # fmt: skip

    def mark_restored(self, project_id: str) -> None:
        """A snapshot was restored: the next one starts a new chain."""
        self.restored.add(project_id)

    def start(self) -> None:
        """Apply retention and move snapshots to a storage node now and then."""
        self.ticker = asyncio.create_task(self._keep_ticking())

    async def _keep_ticking(self) -> None:
        while True:
            try:
                await self.tick()
            except Exception:  # a bad tick must not end the schedule
                log.exception("the snapshot schedule failed")
            await asyncio.sleep(TICK_S)

    async def tick(self) -> tuple[int, int]:
        """Retention, then snapshots kept here go to a storage node; (deleted, moved)."""
        deleted = await self.retain()
        moved = 0
        if await self.store.node() is not None:
            async with self.db.session() as session:
                local = list(await session.scalars(
                    select(ProjectSnapshot.id).where(ProjectSnapshot.state == "stored",
                                                     ProjectSnapshot.node_id.is_(None))
                    .order_by(ProjectSnapshot.created_at).limit(MOVES_PER_TICK)))  # fmt: skip
            for snapshot_id in local:
                moved += await self.store.hand_over(snapshot_id)
        return deleted, moved

    async def retain(self, now: float | None = None) -> int:
        """Delete what retention no longer keeps (never a snapshot a kept one builds on)."""
        now = time.time() if now is None else now
        keep = self.settings.snapshots
        async with self.db.session() as session, session.begin():
            await session.execute(delete(ProjectSnapshot).where(
                ProjectSnapshot.state == "failed",
                ProjectSnapshot.created_at < now - keep.keep_all_h * 3600))  # fmt: skip
            rows = list(await session.scalars(
                select(ProjectSnapshot).where(ProjectSnapshot.state.in_(KEPT))))  # fmt: skip
        by_project: dict[str, list[ProjectSnapshot]] = {}
        for row in rows:
            by_project.setdefault(row.project_id, []).append(row)
        gone = []
        for project_rows in by_project.values():
            chosen = kept(project_rows, now, keep)
            gone += [row for row in project_rows if row.id not in chosen]
        return await self.store.drop(gone)

    async def close(self) -> None:
        """Stop the tick and the waiting snapshots."""
        for task in [*self.tasks.values(), *([self.ticker] if self.ticker else [])]:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
