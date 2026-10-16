"""The worker's side of the server connection: ask for jobs, fetch, run, report.

Like the Mac worker, it only ever connects out (HTTPS to the server), so the Windows machine
needs no open port. While a slot is free it asks for a job; each job runs in its own task.
"""

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator
from pathlib import Path

import httpx

from forge_winworker import __version__
from forge_winworker.build import PACKAGE, SOURCE, msix_job
from forge_winworker.runners import Runner
from forge_winworker.wire import JobOffer, JobResult, PollAnswer, PollRequest

log = logging.getLogger(__name__)


class WorkerClient:
    """Takes jobs from one Forge Web server and runs them with a runner."""

    def __init__(
        self,
        server: str,
        token: str,
        runner: Runner,
        *,
        slots: int = 1,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        headers = {"Authorization": f"Bearer {token}", "X-Forge-Win-Worker": __version__}
        self.http = httpx.AsyncClient(base_url=server.rstrip("/"), headers=headers,
                                      timeout=httpx.Timeout(90, connect=30),
                                      transport=transport)  # fmt: skip
        self.runner, self.slots, self.busy = runner, slots, 0
        self.tasks: set[asyncio.Task[None]] = set()
        self.room = asyncio.Condition()

    async def serve(self, stop: asyncio.Event) -> None:
        """Ask for jobs until `stop` is set; network trouble only slows it down."""
        backoff = 1.0
        try:
            while not stop.is_set():
                await self.wait_for_room(stop)
                if stop.is_set():
                    break
                try:
                    offer = await self.poll_unless(stop)
                except (httpx.HTTPError, ValueError) as err:
                    log.warning("asking the server for work failed: %s", err)
                    await wait_or_stop(stop, backoff)
                    backoff = min(backoff * 2, 60)
                    continue
                backoff = 1.0
                if offer is not None:
                    self.start(offer)
        finally:
            for task in list(self.tasks):
                task.cancel()
            await asyncio.gather(*self.tasks, return_exceptions=True)
            await self.http.aclose()

    async def poll_unless(self, stop: asyncio.Event) -> JobOffer | None:
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

    async def poll(self) -> JobOffer | None:
        """One long poll; a job or None."""
        body = PollRequest(free_slots=self.slots - self.busy, version=__version__)
        reply = await self.http.post("/api/win/poll", content=body.model_dump_json(),
                                     headers={"Content-Type": "application/json"})  # fmt: skip
        reply.raise_for_status()
        return PollAnswer.model_validate_json(reply.content).job

    def start(self, offer: JobOffer) -> None:
        """Run a job in a task of its own."""
        self.busy += 1
        task = asyncio.create_task(self.handle(offer))
        self.tasks.add(task)
        task.add_done_callback(self.done)

    def done(self, task: "asyncio.Task[None]") -> None:
        self.tasks.discard(task)
        self.busy -= 1
        asyncio.get_running_loop().create_task(self.notify())

    async def notify(self) -> None:
        async with self.room:
            self.room.notify_all()

    async def wait_for_room(self, stop: asyncio.Event) -> None:
        """Until a slot is free (or stop)."""
        async with self.room:
            while self.busy >= self.slots and not stop.is_set():
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(self.room.wait(), 5)

    async def handle(self, offer: JobOffer) -> None:
        """Fetch the commit, run the job, send the package and the report."""
        folder: Path | None = None
        try:
            folder = await self.runner.prepare(offer.id)
            await self.download(offer.id, folder / SOURCE)
            if offer.msix is None:
                raise ValueError("an msix job came without its parameters")
            result = await asyncio.wait_for(msix_job(folder, offer.msix, self.runner.run),
                                            offer.timeout_s)  # fmt: skip
            if result.ok:
                await self.upload(offer.id, folder / PACKAGE)
        except TimeoutError:
            result = JobResult(ok=False, error="the job took longer than its time limit")
        except Exception as err:  # the server must hear about every job it handed out
            log.exception("job %s failed", offer.id)
            result = JobResult(ok=False, error=f"the Windows worker could not run the job: {err}")
        try:
            await self.report(offer.id, result)
        except httpx.HTTPError as err:
            log.warning("reporting job %s failed: %s", offer.id, err)
        finally:
            if folder is not None:
                await self.runner.remove(folder)

    async def download(self, job_id: str, target: Path) -> None:
        """The job's packed commit."""
        async with self.http.stream("GET", f"/api/win/jobs/{job_id}/source") as reply:
            reply.raise_for_status()
            with target.open("wb") as out:
                async for chunk in reply.aiter_bytes():
                    out.write(chunk)

    async def upload(self, job_id: str, package: Path) -> None:
        """The package a job made (it does not stay on this machine)."""
        reply = await self.http.post(f"/api/win/jobs/{job_id}/package", content=chunks(package),
                                     headers={"Content-Type": "application/octet-stream"},
                                     timeout=httpx.Timeout(3600, connect=30))  # fmt: skip
        reply.raise_for_status()

    async def report(self, job_id: str, result: JobResult) -> None:
        """How the job ended."""
        reply = await self.http.post(f"/api/win/jobs/{job_id}/result",
                                     content=result.model_dump_json(),
                                     headers={"Content-Type": "application/json"})  # fmt: skip
        if reply.status_code == 409:
            log.warning("the server no longer waits for job %s", job_id)
            return
        reply.raise_for_status()


async def chunks(path: Path) -> AsyncIterator[bytes]:
    """A file in pieces, read off the event loop."""
    with path.open("rb") as file:
        while chunk := await asyncio.to_thread(file.read, 1024 * 1024):
            yield chunk


async def wait_or_stop(stop: asyncio.Event, seconds: float) -> None:
    """Sleep, unless stop is set first."""
    with contextlib.suppress(TimeoutError):
        await asyncio.wait_for(stop.wait(), seconds)
