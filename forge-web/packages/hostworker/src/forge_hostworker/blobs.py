"""A storage node's blobs (W66): sealed files Forge Web hands over (a project's snapshots), kept
at `<data>/blobs/<owner>/<id>` (0600), fetched and sent back only over the node's own outbound
connection. The node never opens them: Forge Web keeps the keys.

A quota keeps the disk from filling: at least `keep_free` of the disk stays free, and with
`max_bytes` the blobs together take at most that much.
"""

import asyncio
import hashlib
import os
import shutil
from collections.abc import AsyncIterator
from pathlib import Path
from typing import BinaryIO

import httpx

from forge_hostworker.blob_wire import BlobRef, blob_path
from forge_hostworker.wire import HostJob, JobResult

PART = 1024 * 1024
TRANSFER_TIMEOUT = httpx.Timeout(3600, connect=30)


class BlobStore:
    """The blobs on this node and the room left for more."""

    def __init__(self, root: Path, *, keep_free: float = 0.10, max_bytes: int = 0) -> None:
        self.root = root
        self.keep_free = keep_free  # share of the disk that stays free
        self.max_bytes = max_bytes  # 0: no limit beyond the free share

    def used(self) -> int:
        """Bytes the blobs take."""
        if not self.root.is_dir():
            return 0
        return sum(p.stat().st_size for p in self.root.rglob("*") if p.is_file())

    def room(self) -> int:
        """Bytes one more blob may take."""
        probe = self.root if self.root.exists() else self.root.parent
        disk = shutil.disk_usage(probe if probe.exists() else Path.home())
        room = int(disk.free - disk.total * self.keep_free)
        if self.max_bytes:
            room = min(room, self.max_bytes - self.used())
        return max(room, 0)

    async def run(self, http: httpx.AsyncClient, job: HostJob) -> JobResult:
        """Run one blob job."""
        blob = job.blob
        if blob is None:
            return JobResult(ok=False, error="the job names no blob")
        if job.kind == "blob_put":
            return await self.put(http, blob)
        if job.kind == "blob_get":
            return await self.get(http, blob)
        path = blob_path(self.root, blob)
        await asyncio.to_thread(path.unlink, True)
        return JobResult(ok=True)

    async def put(self, http: httpx.AsyncClient, blob: BlobRef) -> JobResult:
        """Fetch a blob from Forge Web and keep it, if it fits and arrives whole."""
        path = blob_path(self.root, blob)
        if path.is_file() and await asyncio.to_thread(digest, path) == blob.sha256:
            return JobResult(ok=True)  # kept already (the answer to an earlier try was lost)
        if blob.size > await asyncio.to_thread(self.room):
            return JobResult(ok=False, error="no room for the blob on this node",
                             hint="free disk space or give the node a larger quota")  # fmt: skip
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        partial = path.with_name(f"{blob.id}.part")
        try:
            problem = await self.fetch(http, blob, partial)
            if problem:
                return JobResult(ok=False, error=problem)
            partial.replace(path)
        finally:
            partial.unlink(missing_ok=True)
        return JobResult(ok=True)

    async def fetch(self, http: httpx.AsyncClient, blob: BlobRef, target: Path) -> str:
        """Download into `target`; why it is not the blob ("" when it is)."""
        hasher, size = hashlib.sha256(), 0
        fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as out:
            async with http.stream("GET", f"/api/host/blob/{blob.id}",
                                   timeout=TRANSFER_TIMEOUT) as reply:  # fmt: skip
                reply.raise_for_status()
                async for chunk in reply.aiter_bytes(PART):
                    size += len(chunk)
                    if size > blob.size:
                        return "the blob is larger than announced"
                    hasher.update(chunk)
                    await asyncio.to_thread(out.write, chunk)
        if size != blob.size or hasher.hexdigest() != blob.sha256:
            return "the blob arrived changed or cut off"
        return ""

    async def get(self, http: httpx.AsyncClient, blob: BlobRef) -> JobResult:
        """Send a kept blob back to Forge Web."""
        path = blob_path(self.root, blob)
        if not path.is_file():
            return JobResult(ok=False, error="the blob is not on this node")
        with path.open("rb") as sealed:
            reply = await http.put(
                f"/api/host/blob/{blob.id}", content=parts(sealed),
                headers={"Content-Type": "application/octet-stream"}, timeout=TRANSFER_TIMEOUT,
            )  # fmt: skip
        if reply.status_code >= 400:
            return JobResult(ok=False, error=f"Forge Web refused the blob ({reply.status_code})")
        return JobResult(ok=True)


def digest(path: Path) -> str:
    """The SHA-256 of a file."""
    hasher = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(PART):
            hasher.update(chunk)
    return hasher.hexdigest()


async def parts(handle: BinaryIO) -> AsyncIterator[bytes]:
    """A file's bytes in parts, for an upload that does not fit in memory."""
    while chunk := await asyncio.to_thread(handle.read, PART):
        yield chunk
