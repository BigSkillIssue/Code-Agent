"""Where snapshots are kept: sealed with the project's own key (made once, kept in the vault),
handed to the storage node with the most room (`blob_put`), or kept in
`<data>/snapshots/<project>/<id>` while there is none; fetched back for a restore (`blob_get`),
and removed by retention (`blob_delete`).

A node only ever holds sealed bytes; it fetches a blob only while it is being handed to it, and
sends one back only while Forge Web waits for it.
"""

import asyncio
import base64
import hashlib
import json
import logging
import os
import secrets
import time
from collections.abc import AsyncIterator, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import select

from forge_hostworker.blob_wire import BLOB_APP, BlobKind, BlobRef
from forge_hostworker.wire import HostJob, decrypt_chunks, encrypt_file
from forge_web.db.engine import Database
from forge_web.db.hosting_models import HostServer
from forge_web.db.snapshot_models import ProjectSnapshot, ProjectSnapshotKey
from forge_web.hosting.hosts import ONLINE_S, HostQueue, roles_of
from forge_web.settings import WebSettings
from forge_web.snapshots.settings import SnapshotSettings
from forge_web.vault import Vault

log = logging.getLogger(__name__)
BLOB_TIMEOUT_S = 3600.0
PART = 1024 * 1024
KEPT = ("pending", "stored")  # states of a snapshot that exists somewhere


class SnapshotProblem(Exception):
    """A snapshot cannot be made, kept or brought back; the message says why."""


@dataclass
class Expected:
    """A blob a node is to send back for a restore."""

    host_id: str
    target: Path
    size: int
    sha256: str


class SnapshotStore:
    """The sealed snapshots on this server and on the storage nodes."""

    def __init__(self, db: Database, vault: Vault, settings: WebSettings, hosts: HostQueue):
        self.db, self.vault, self.settings, self.hosts = db, vault, settings, hosts
        self.root = settings.data_dir / "snapshots"
        self.expected: dict[str, Expected] = {}  # blob id -> what a blob_get will upload

    def path(self, project_id: str, snapshot_id: str) -> Path:
        """Where this server keeps a sealed snapshot (ids are generated here, never taken in)."""
        return self.root / project_id / snapshot_id

    async def key(self, project_id: str) -> bytes:
        """The project's snapshot key, made the first time."""
        async with self.db.session() as session, session.begin():
            row = await session.get(ProjectSnapshotKey, project_id)
            if row is None:
                key = os.urandom(32)
                session.add(ProjectSnapshotKey(
                    project_id=project_id, created_at=time.time(),
                    key=self.vault.encrypt(base64.b64encode(key).decode())))  # fmt: skip
                return key
            return base64.b64decode(self.vault.decrypt(row.key))

    async def seal(self, project_id: str, snapshot_id: str, plain: Path) -> tuple[int, str]:
        """Seal a bundle into this server's store; the sealed file's size and SHA-256."""
        key = await self.key(project_id)
        target = self.path(project_id, snapshot_id)
        target.parent.mkdir(parents=True, exist_ok=True)
        await asyncio.to_thread(encrypt_file, key, plain, target)
        return target.stat().st_size, await asyncio.to_thread(digest, target)

    async def node(self) -> HostServer | None:
        """The enabled, online storage node with the most free disk (None: there is none)."""
        if not self.settings.hosting.enabled:
            return None
        recent = time.time() - ONLINE_S
        async with self.db.session() as session:
            rows = await session.scalars(select(HostServer).where(
                HostServer.enabled, HostServer.last_seen > recent))  # fmt: skip
            nodes = [row for row in rows if "storage" in roles_of(row)]
        return max(nodes, key=lambda row: row.disk_free_gb, default=None)

    async def hand_over(self, snapshot_id: str) -> bool:
        """Give a snapshot kept here to a storage node; True once the node has it."""
        node = await self.node()
        async with self.db.session() as session, session.begin():
            row = await session.get(ProjectSnapshot, snapshot_id)
            if node is None or row is None or row.state != "stored" or row.node_id:
                return False
            row.state, row.node_id = "pending", node.id
            blob = blob_of(row)
        job = blob_job("blob_put", blob)
        await self.hosts.send(node.id, snapshot_id, job)
        result = await self.hosts.result(job.id)
        async with self.db.session() as session, session.begin():
            row = await session.get(ProjectSnapshot, snapshot_id)
            if row is None:
                return False
            row.state = "stored"
            row.node_id = node.id if result.ok else None
            row.error = "" if result.ok else f"the storage node did not keep it: {result.error}"
        if result.ok:
            await asyncio.to_thread(self.path(blob.owner, blob.id).unlink, True)
        else:
            log.warning("snapshot %s stays on this server: %s", snapshot_id, result.error)
        return result.ok

    async def handed(self, host_id: str, blob_id: str) -> Path:
        """The sealed file a node fetches: only a snapshot being handed to that node."""
        async with self.db.session() as session:
            row = await session.get(ProjectSnapshot, blob_id)
        path = self.path(row.project_id, row.id) if row is not None else None
        if row is None or row.state != "pending" or row.node_id != host_id or path is None:
            raise LookupError("this blob is not being handed to this node")
        if not path.is_file():
            raise LookupError("this blob is not on the server")
        return path

    async def receive(self, host_id: str, blob_id: str, parts: AsyncIterator[bytes]) -> int:
        """Keep what a node sends back for a restore; LookupError when nobody asked for it,
        ValueError when it is not the blob."""
        wanted = self.expected.get(blob_id)
        if wanted is None or wanted.host_id != host_id:
            raise LookupError("this blob was not asked for")
        partial, hasher, size = wanted.target.with_suffix(".part"), hashlib.sha256(), 0
        try:
            with partial.open("wb") as out:
                async for part in parts:
                    size += len(part)
                    if size > wanted.size:
                        raise ValueError("the blob is larger than the one kept")
                    hasher.update(part)
                    await asyncio.to_thread(out.write, part)
            if size != wanted.size or hasher.hexdigest() != wanted.sha256:
                raise ValueError("the blob came back changed")
            partial.replace(wanted.target)
        finally:
            partial.unlink(missing_ok=True)
        return size

    async def sealed(self, row: ProjectSnapshot, work: Path) -> Path:
        """The sealed file of a snapshot: here, or fetched from its node into `work`."""
        local = self.path(row.project_id, row.id)
        if local.is_file():
            return local
        if row.node_id is None:
            raise SnapshotProblem("the snapshot's file is missing on this server")
        target = work / f"{row.id}.sealed"
        self.expected[row.id] = Expected(row.node_id, target, row.size, row.sha256)
        try:
            job = blob_job("blob_get", blob_of(row))
            await self.hosts.send(row.node_id, row.id, job)
            timeout = self.settings.snapshots.fetch_timeout_s
            result = await asyncio.wait_for(self.hosts.result(job.id), timeout)
        except TimeoutError:
            raise SnapshotProblem("the storage node did not answer in time") from None
        finally:
            self.expected.pop(row.id, None)
        if not result.ok or not target.is_file():
            raise SnapshotProblem(f"the storage node did not send it: {result.error}")
        return target

    async def plain_parts(self, project_id: str, sealed: Path) -> AsyncIterator[bytes]:
        """The bundle inside a sealed file, part by part (raises if it does not open)."""
        chunks = decrypt_chunks(await self.key(project_id), sealed)
        while (part := await asyncio.to_thread(next, chunks, None)) is not None:
            yield part

    async def chain(self, project_id: str, snapshot_id: str) -> list[ProjectSnapshot]:
        """The snapshots a restore reads, the full one first."""
        found: list[ProjectSnapshot] = []
        async with self.db.session() as session:
            wanted: str | None = snapshot_id
            while wanted is not None and len(found) <= 10_000:
                row = await session.get(ProjectSnapshot, wanted)
                if row is None or row.project_id != project_id or row.state not in KEPT:
                    raise SnapshotProblem("a snapshot this one builds on is no longer kept")
                found.append(row)
                wanted = row.parent_id if row.kind == "incr" else None
        return found[::-1]

    async def drop(self, rows: Iterable[ProjectSnapshot]) -> int:
        """Delete snapshots here and on their nodes; how many."""
        count = 0
        for row in rows:
            await asyncio.to_thread(self.path(row.project_id, row.id).unlink, True)
            if row.node_id and row.state == "stored":
                await self.hosts.send(row.node_id, row.id, blob_job("blob_delete", blob_of(row)))
            async with self.db.session() as session, session.begin():
                stored = await session.get(ProjectSnapshot, row.id)
                if stored is not None:
                    stored.state = "deleted"
            count += 1
        return count


def blob_of(row: ProjectSnapshot) -> BlobRef:
    """A snapshot as a node's blob."""
    return BlobRef(id=row.id, owner=row.project_id, size=row.size, sha256=row.sha256)


def blob_job(kind: BlobKind, blob: BlobRef) -> HostJob:
    """A blob job for a storage node."""
    return HostJob(id=secrets.token_hex(16), kind=kind, app=BLOB_APP, environment="production",
                   blob=blob, timeout_s=BLOB_TIMEOUT_S)  # fmt: skip


def digest(path: Path) -> str:
    """The SHA-256 of a file."""
    hasher = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(PART):
            hasher.update(chunk)
    return hasher.hexdigest()


def kept(rows: list[ProjectSnapshot], now: float, keep: SnapshotSettings) -> set[str]:
    """The snapshots retention keeps: all of the last `keep_all_h`, the newest of each day for
    `keep_daily_days`, of each week for `keep_weekly_weeks`, the newest one always, and every
    snapshot a kept one builds on."""
    live = sorted((r for r in rows if r.state in KEPT), key=lambda r: r.created_at)
    chosen = {live[-1].id} if live else set()
    days: dict[str, str] = {}
    weeks: dict[str, str] = {}
    for row in live:  # oldest first: the newest of a day or week wins
        age, when = now - row.created_at, datetime.fromtimestamp(row.created_at, UTC)
        if row.state == "pending" or age <= keep.keep_all_h * 3600:
            chosen.add(row.id)
        elif age <= keep.keep_daily_days * 86400:
            days[when.strftime("%Y-%m-%d")] = row.id
        elif age <= keep.keep_weekly_weeks * 7 * 86400:
            weeks[json.dumps(when.isocalendar()[:2])] = row.id
    chosen |= set(days.values()) | set(weeks.values())
    parents = {row.id: row.parent_id for row in live if row.kind == "incr"}
    for start in list(chosen):
        parent = parents.get(start)
        while parent is not None and parent not in chosen:
            chosen.add(parent)
            parent = parents.get(parent)
    return chosen
