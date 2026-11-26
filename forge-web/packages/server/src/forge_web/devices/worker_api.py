"""The workers' side of device sessions (W60), with the workers' own tokens: a host takes the
Android sessions of its own apps (`/api/host/devices/...`), a Windows build machine takes Windows
sessions (`/api/win/devices/...`; signing machines never do). A worker only ever reaches its own
sessions: it fetches the packed commit, answers the start (action 0), asks for the next action
and answers it.
"""

from collections.abc import Awaitable, Callable
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import ValidationError

from forge_hostworker.device_wire import DeviceAnswer, DeviceNext, DevicePoll
from forge_web.devices.sessions import DeviceRefused, DeviceSession, WorkerKind
from forge_web.hosting.deploy_api import current_host
from forge_web.services import services_of
from forge_web.stores.windows_api import current_worker

POLL_WAIT_S = 25.0
MAX_ANSWER_BYTES = 9 * 1024 * 1024  # a screenshot, base64, and a UI tree


async def host_id(request: Request) -> str:
    """The calling host's id (401 without a host token)."""
    return (await current_host(request)).id


async def win_id(request: Request) -> str:
    """The calling Windows build machine's id (401, or 403 for a signing machine)."""
    worker = await current_worker(request)
    if worker.signs:
        raise HTTPException(403, "signing machines run no devices")
    return worker.id


def device_worker_router() -> APIRouter:
    """Both workers' device routes."""
    router = APIRouter()
    routes(router, "/api/host/devices", "host", host_id)
    routes(router, "/api/win/devices", "win", win_id)
    return router


def routes(
    router: APIRouter, prefix: str, kind: WorkerKind, who: Callable[[Request], Awaitable[str]]
) -> None:
    """One worker kind's routes under `prefix`."""

    def own(request: Request, worker_id: str, session_id: str) -> DeviceSession:
        try:
            return services_of(request).devices.of_worker(kind, worker_id, session_id)
        except DeviceRefused as refusal:
            raise HTTPException(409, str(refusal)) from None

    @router.post(prefix + "/poll")
    async def poll(request: Request, worker_id: str = Depends(who)) -> DevicePoll:
        devices = services_of(request).devices
        return DevicePoll(session=await devices.claim(kind, worker_id, POLL_WAIT_S))

    @router.get(prefix + "/{session_id}/source")
    async def source(session_id: str, request: Request,
                     worker_id: str = Depends(who)) -> FileResponse:  # fmt: skip
        session = own(request, worker_id, session_id)
        path = services_of(request).devices.source(session)
        if session.status != "starting" or not path.is_file():
            raise HTTPException(409, "this session's commit is gone")
        return FileResponse(path, media_type="application/octet-stream")

    @router.post(prefix + "/{session_id}/next")
    async def next_action(session_id: str, request: Request,
                          worker_id: str = Depends(who)) -> DeviceNext:  # fmt: skip
        session = own(request, worker_id, session_id)
        return await services_of(request).devices.next_action(session, POLL_WAIT_S)

    @router.post(prefix + "/{session_id}/answer")
    async def answer(session_id: str, request: Request,
                     worker_id: str = Depends(who)) -> dict[str, Any]:  # fmt: skip
        session = own(request, worker_id, session_id)
        found = await read_answer(request)
        await services_of(request).devices.answered(session, found)
        return {"ended": session.status == "ended"}

    @router.post(prefix + "/{session_id}/ended")
    async def ended(session_id: str, request: Request,
                    worker_id: str = Depends(who)) -> dict[str, bool]:  # fmt: skip
        session = own(request, worker_id, session_id)
        found = await read_answer(request)
        await services_of(request).devices.end(session, found.error or "the device stopped",
                                               found.hint)  # fmt: skip
        return {"ok": True}


async def read_answer(request: Request) -> DeviceAnswer:
    """The worker's answer, within its size limit."""
    raw = bytearray()
    async for chunk in request.stream():
        raw += chunk
        if len(raw) > MAX_ANSWER_BYTES:
            raise HTTPException(413, "the answer is too large")
    try:
        return DeviceAnswer.model_validate_json(bytes(raw))
    except ValidationError as err:
        raise HTTPException(422, f"not a valid answer: {err.errors()[0]['msg']}") from None
