"""The worker's side of the server connection: ask for jobs, fetch, run, report.

The worker only ever connects out (HTTPS to the server), so the host needs no port open to
Forge Web. The server hands out one job per app at a time; jobs of different apps run side by side.
"""

import asyncio
import contextlib
import json
import logging
import tempfile
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path
from typing import BinaryIO

import httpx
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey

from forge_hostworker import __version__
from forge_hostworker.android import bundle_path
from forge_hostworker.backup import outgoing
from forge_hostworker.blob_wire import BLOB_KINDS
from forge_hostworker.blobs import BlobStore
from forge_hostworker.device_client import DeviceClient
from forge_hostworker.mesh import MeshNode
from forge_hostworker.runner import HostRunner
from forge_hostworker.wire import (
    WITH_SECRETS,
    WITH_SOURCE,
    HostJob,
    HostUsage,
    JobResult,
    PollAnswer,
    PollRequest,
    SealedSecrets,
    open_sealed,
)

UPLOADS = {"archive": "archive", "android": "artifact"}  # what a job uploads, and where
log = logging.getLogger(__name__)
USAGE_EVERY_S = 300.0
NOT_APPS = "this host does not have the apps role, so it runs no apps"
NOT_STORAGE = "this host does not have the storage role, so it keeps no blobs"


class HostClient:
    """Takes jobs from one Forge Web server and runs them on this host."""

    def __init__(
        self,
        server: str,
        token: str,
        runner: HostRunner,
        key: X25519PrivateKey,
        *,
        slots: int = 2,
        transport: httpx.AsyncBaseTransport | None = None,
        devices: bool = False,
        mesh: MeshNode | None = None,
        blobs: BlobStore | None = None,
    ) -> None:
        headers = {"Authorization": f"Bearer {token}", "X-Forge-Host-Worker": __version__}
        self.http = httpx.AsyncClient(
            base_url=server.rstrip("/"),
            headers=headers,
            timeout=httpx.Timeout(90, connect=30),
            transport=transport,
        )
        self.runner = runner
        self.key = key
        self.slots = slots
        self.tasks: set[asyncio.Task[None]] = set()
        self.running: set[str] = set()  # the ids of the jobs in `tasks`
        self.usage_every_s = USAGE_EVERY_S
        self.devices = devices  # W61: this host runs Android emulators for the agent (KVM)
        self.roles: list[str] = []  # the host's roles, as the server's last answer names them
        self.mesh = mesh  # W65: keeps forge0 as the server's view says (None: not here)
        self.mesh_revision = 0  # the network's revision in the server's last answer
        self.blobs = blobs or BlobStore(Path(runner.data_dir) / "blobs")  # the storage role's

    async def serve(self, stop: asyncio.Event) -> None:
        """Ask for jobs until `stop` is set; network trouble only slows it down. The loops of
        the host's roles start once the server's first answer names them."""
        backoff = 1.0
        side: list[asyncio.Task[None]] | None = None
        try:
            while not stop.is_set():
                if len(self.tasks) >= self.slots:
                    await asyncio.wait(self.tasks, return_when=asyncio.FIRST_COMPLETED)
                    continue
                try:
                    job = await self.poll_unless(stop)
                except (httpx.HTTPError, ValueError) as err:
                    log.warning("asking the server for work failed: %s", err)
                    await wait_or_stop(stop, backoff)
                    backoff = min(backoff * 2, 60)
                    continue
                backoff = 1.0
                if side is None and self.roles:
                    side = self.role_loops(stop)
                if self.mesh is not None:  # every role is in the network
                    await self.mesh.sync(self.http, self.mesh_revision)
                if job is not None:
                    self.start(job)
        finally:
            loops = [*(side or []), *self.tasks]
            for task in loops:
                task.cancel()
            await asyncio.gather(*loops, return_exceptions=True)
            await self.http.aclose()

    def role_loops(self, stop: asyncio.Event) -> list[asyncio.Task[None]]:
        """The loops of the host's roles: reporting what the apps use, and the Android emulators
        where `--devices` asks for them; a storage node reports the room it has for blobs."""
        others = [role for role in self.roles if role not in ("apps", "storage")]
        if others:
            log.info("this host's roles %s have nothing to run yet", ", ".join(others))
        if "apps" not in self.roles:
            if self.devices:
                log.warning("--devices needs the apps role; no emulators run on this host")
            if "storage" in self.roles:
                return [asyncio.create_task(self.report_usage(stop, self.storage_usage))]
            return []
        loops = [asyncio.create_task(self.report_usage(stop))]
        if self.devices:
            loops.append(asyncio.create_task(DeviceClient(self.http, self.runner).serve(stop)))
        return loops

    async def storage_usage(self) -> HostUsage:
        """A storage node's report: no apps, and the room left for blobs."""
        return HostUsage(disk_free_gb=await asyncio.to_thread(self.blobs.room) / 1e9)

    async def report_usage(
        self, stop: asyncio.Event, measure: Callable[[], Awaitable[HostUsage]] | None = None
    ) -> None:
        """Tell the server what the apps use, every few minutes (trouble is only logged)."""
        while not stop.is_set():
            try:
                found = await (measure or self.runner.usage)()
                reply = await self.http.post(
                    "/api/host/metrics", content=found.model_dump_json(),
                    headers={"Content-Type": "application/json"},
                )  # fmt: skip
                reply.raise_for_status()
            except (httpx.HTTPError, OSError, ValueError) as err:
                log.warning("reporting what the apps use failed: %s", err)
            await wait_or_stop(stop, self.usage_every_s)

    async def poll_unless(self, stop: asyncio.Event) -> HostJob | None:
        """One long poll, given up as soon as `stop` is set."""
        polling = asyncio.ensure_future(self.poll())
        stopping = asyncio.ensure_future(stop.wait())
        try:
            await asyncio.wait({polling, stopping}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            stopping.cancel()
        if not polling.done():
            polling.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await polling
            return None
        return polling.result()

    async def poll(self) -> HostJob | None:
        """One long poll; a job or None (and the host's roles, kept in `roles`)."""
        free = max(self.slots - len(self.tasks), 1)
        body = PollRequest(free_slots=free, version=__version__, running=sorted(self.running))
        reply = await self.http.post(
            "/api/host/poll",
            content=body.model_dump_json(),
            headers={"Content-Type": "application/json"},
        )
        reply.raise_for_status()
        answer = PollAnswer.model_validate_json(reply.content)
        self.roles = list(answer.roles)
        self.mesh_revision = answer.mesh_revision
        return answer.job

    def start(self, job: HostJob) -> None:
        """Run a job in a task of its own."""
        task = asyncio.create_task(self.handle(job))
        self.tasks.add(task)
        self.running.add(job.id)
        task.add_done_callback(self.tasks.discard)
        task.add_done_callback(lambda _: self.running.discard(job.id))

    async def handle(self, job: HostJob) -> None:
        """Fetch what the job needs, run it, report how it went."""
        if job.kind in BLOB_KINDS:
            await self.handle_blob(job)
            return
        if "apps" not in self.roles:  # every other kind of job is an app's
            with contextlib.suppress(httpx.HTTPError):
                await self.report(job.id, JobResult(ok=False, error=NOT_APPS))
            return
        with tempfile.TemporaryDirectory(prefix="forge-host-") as scratch:
            try:
                source, values = None, {}
                if job.kind in WITH_SOURCE:
                    source = Path(scratch) / "source.tar.gz"
                    await self.download(job.id, source)
                if job.kind in WITH_SECRETS:
                    values = await self.secrets(job.id)
                result = await asyncio.wait_for(self.runner.run(job, source, values), job.timeout_s)
                if job.kind in UPLOADS and result.ok:
                    await self.upload(job)
            except TimeoutError:
                result = JobResult(ok=False, error="the job took longer than its time limit")
            except Exception as err:  # the server must hear about every job it handed out
                log.exception("job %s failed", job.id)
                result = JobResult(ok=False, error=f"the host could not run the job: {err}")
        try:
            await self.report(job.id, result)
        except httpx.HTTPError as err:
            log.warning("reporting job %s failed: %s", job.id, err)

    async def handle_blob(self, job: HostJob) -> None:
        """A storage node's job: keep, send back or remove a blob."""
        try:
            if "storage" not in self.roles:
                result = JobResult(ok=False, error=NOT_STORAGE)
            else:
                result = await asyncio.wait_for(self.blobs.run(self.http, job), job.timeout_s)
        except TimeoutError:
            result = JobResult(ok=False, error="the job took longer than its time limit")
        except Exception as err:  # the server must hear about every job it handed out
            log.exception("blob job %s failed", job.id)
            result = JobResult(ok=False, error=f"the node could not run the job: {err}")
        try:
            await self.report(job.id, result)
        except httpx.HTTPError as err:
            log.warning("reporting job %s failed: %s", job.id, err)

    async def download(self, job_id: str, target: Path) -> None:
        """The release's packed source."""
        async with self.http.stream("GET", f"/api/host/jobs/{job_id}/source") as reply:
            reply.raise_for_status()
            with target.open("wb") as out:
                async for chunk in reply.aiter_bytes():
                    out.write(chunk)

    async def upload(self, job: HostJob) -> None:
        """Send what a job made to Forge Web: an archive job's encrypted backup, an android
        job's bundle (neither stays on the host)."""
        made = bundle_path if job.kind == "android" else outgoing
        path = made(self.runner.data_dir, job.id)
        try:
            with path.open("rb") as sealed:
                reply = await self.http.post(
                    f"/api/host/jobs/{job.id}/{UPLOADS[job.kind]}", content=chunks(sealed),
                    headers={"Content-Type": "application/octet-stream"},
                    timeout=httpx.Timeout(3600, connect=30),
                )  # fmt: skip
            reply.raise_for_status()
        finally:
            path.unlink(missing_ok=True)

    async def secrets(self, job_id: str) -> dict[str, str]:
        """The release's secrets, sealed to this host's key (the server hands them out once)."""
        reply = await self.http.get(f"/api/host/jobs/{job_id}/secrets")
        reply.raise_for_status()
        box = SealedSecrets.model_validate_json(reply.content)
        values = json.loads(open_sealed(self.key, box))
        if not isinstance(values, dict) or not all(isinstance(v, str) for v in values.values()):
            raise ValueError("the secrets are not names and values")
        return {str(k): v for k, v in values.items()}

    async def report(self, job_id: str, result: JobResult) -> None:
        """How the job ended."""
        reply = await self.http.post(
            f"/api/host/jobs/{job_id}/result",
            content=result.model_dump_json(),
            headers={"Content-Type": "application/json"},
        )
        if reply.status_code == 409:
            log.warning("the server no longer waits for job %s", job_id)
            return
        reply.raise_for_status()


async def wait_or_stop(stop: asyncio.Event, seconds: float) -> None:
    """Sleep, unless stop is set first."""
    with contextlib.suppress(TimeoutError):
        await asyncio.wait_for(stop.wait(), seconds)


async def chunks(file: BinaryIO) -> AsyncIterator[bytes]:
    """A file's bytes in parts, for an upload that does not fit in memory."""
    while part := file.read(1024 * 1024):
        yield part
