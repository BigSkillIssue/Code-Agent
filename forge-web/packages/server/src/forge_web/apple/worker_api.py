"""The API Mac workers use (`/api/mac/...`), with their own tokens instead of a sign-in.

A worker asks for a job (long poll), downloads the job's packed project, may upload an archive,
and reports the result. It only ever sees its own running jobs.
"""

from collections.abc import Awaitable
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import ValidationError

from forge_macworker.wire import JobResult, PollAnswer, PollRequest, SigningMaterial
from forge_web.apple.jobs import JobRefused
from forge_web.apple.workers import worker_of
from forge_web.db.models import MacWorker
from forge_web.services import services_of

POLL_WAIT_S = 25.0
MAX_RESULT_BYTES = 20 * 1024 * 1024  # a screenshot is the largest part


async def current_worker(request: Request) -> MacWorker:
    """The Mac worker the request's token belongs to; 401 otherwise."""
    services = services_of(request)
    version = request.headers.get("x-forge-mac-worker", "")[:40]
    worker = await worker_of(services.db, request.headers.get("authorization", ""), version)
    if worker is None:
        raise HTTPException(401, "not a valid Mac worker token")
    if not services.settings.apple.enabled:
        raise HTTPException(403, "Apple builds are turned off on this server")
    return worker


Worker = Annotated[MacWorker, Depends(current_worker)]


def worker_router() -> APIRouter:
    """Routes for Mac workers."""
    router = APIRouter(prefix="/api/mac")

    @router.post("/poll")
    async def poll(body: PollRequest, worker: Worker, request: Request) -> PollAnswer:
        jobs = services_of(request).apple
        return PollAnswer(job=await jobs.claim(worker.id, POLL_WAIT_S, body.version))

    @router.get("/jobs/{job_id}/source")
    async def source(job_id: str, worker: Worker, request: Request) -> FileResponse:
        jobs = services_of(request).apple
        await refused_unless(jobs.running(worker.id, job_id))
        return FileResponse(jobs.source(job_id), media_type="application/gzip")

    @router.get("/jobs/{job_id}/signing")
    async def signing(job_id: str, worker: Worker, request: Request) -> SigningMaterial:
        return await refused_unless(services_of(request).apple.signing_of(worker.id, job_id))

    @router.post("/jobs/{job_id}/archive")
    async def archive(job_id: str, worker: Worker, request: Request) -> dict[str, bool]:
        await refused_unless(services_of(request).apple.store_archive(
            worker.id, job_id, request.stream()))  # fmt: skip
        return {"ok": True}

    @router.post("/jobs/{job_id}/result")
    async def result(job_id: str, worker: Worker, request: Request) -> dict[str, bool]:
        raw = bytearray()
        async for chunk in request.stream():
            raw += chunk
            if len(raw) > MAX_RESULT_BYTES:
                raise HTTPException(413, "the result is too large")
        try:
            report = JobResult.model_validate_json(bytes(raw))
        except ValidationError as err:
            raise HTTPException(422, f"not a valid result: {err.errors()[0]['msg']}") from None
        await refused_unless(services_of(request).apple.finish(worker.id, job_id, report))
        return {"ok": True}

    return router


async def refused_unless[T](work: Awaitable[T]) -> T:
    """Run the job operation; a refusal becomes 409."""
    try:
        return await work
    except JobRefused as refusal:
        raise HTTPException(409, str(refusal)) from None
