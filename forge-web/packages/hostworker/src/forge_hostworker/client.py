"""The worker's side of the server connection: ask for jobs, fetch, run, report.

The worker only ever connects out (HTTPS to the server), so the host needs no port open to
Forge Web. The server hands out one job per app at a time; jobs of different apps run side by side.
"""

import asyncio
import contextlib
import json
import logging
import tempfile
from pathlib import Path

import httpx
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey

from forge_hostworker import __version__
from forge_hostworker.runner import HostRunner
from forge_hostworker.wire import (
    WITH_SOURCE,
    HostJob,
    JobResult,
    PollAnswer,
    PollRequest,
    SealedSecrets,
    open_sealed,
)

log = logging.getLogger(__name__)


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

    async def serve(self, stop: asyncio.Event) -> None:
        """Ask for jobs until `stop` is set; network trouble only slows it down."""
        backoff = 1.0
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
                if job is not None:
                    self.start(job)
        finally:
            for task in list(self.tasks):
                task.cancel()
            await asyncio.gather(*self.tasks, return_exceptions=True)
            await self.http.aclose()

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
        """One long poll; a job or None."""
        body = PollRequest(free_slots=max(self.slots - len(self.tasks), 1), version=__version__)
        reply = await self.http.post(
            "/api/host/poll",
            content=body.model_dump_json(),
            headers={"Content-Type": "application/json"},
        )
        reply.raise_for_status()
        return PollAnswer.model_validate_json(reply.content).job

    def start(self, job: HostJob) -> None:
        """Run a job in a task of its own."""
        task = asyncio.create_task(self.handle(job))
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)

    async def handle(self, job: HostJob) -> None:
        """Fetch what the job needs, run it, report how it went."""
        with tempfile.TemporaryDirectory(prefix="forge-host-") as scratch:
            try:
                source, values = None, {}
                if job.kind in WITH_SOURCE:
                    source = Path(scratch) / "source.tar.gz"
                    await self.download(job.id, source)
                if job.kind == "release":
                    values = await self.secrets(job.id)
                result = await asyncio.wait_for(self.runner.run(job, source, values), job.timeout_s)
            except TimeoutError:
                result = JobResult(ok=False, error="the job took longer than its time limit")
            except Exception as err:  # the server must hear about every job it handed out
                log.exception("job %s failed", job.id)
                result = JobResult(ok=False, error=f"the host could not run the job: {err}")
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
