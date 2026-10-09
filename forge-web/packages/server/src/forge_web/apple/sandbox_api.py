"""What a chat's sandbox calls to build on a Mac: `POST /apple/build` and `/apple/screenshot` on the
gateway (its private socket, reached through the daemon's `forward` channel), with the chat's
run token. The body is the packed project (tar.gz); parameters are in the query.
"""

import secrets
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse, Response
from forge.ports import AppleAction, ApplePlatform

from forge_macworker.wire import BuildParams, JobResult, ScreenshotParams
from forge_web.apple.jobs import AppleJobs, NewJob
from forge_web.apple.workers import any_online
from forge_web.db.models import User
from forge_web.gateway.proxy import Gateway, Refusal
from forge_web.settings import AppleSettings

GZIP = b"\x1f\x8b"


def problem(status: int, error: str, hint: str = "") -> JSONResponse:
    """An error the sandbox's builder turns into a message for the agent."""
    return JSONResponse({"error": error, "hint": hint}, status_code=status)


def apple_routes(gateway: Gateway, jobs: AppleJobs, settings: AppleSettings) -> APIRouter:
    """The gateway's Apple endpoints."""
    router = APIRouter()

    async def submit(request: Request, params: BuildParams | ScreenshotParams) -> Response:
        try:
            chat, user = await gateway.caller(request)
        except Refusal as refusal:
            return problem(refusal.status, str(refusal))
        refused = await may_build(jobs, settings, user)
        if refused is not None:
            return refused
        source = jobs.root / "incoming" / f"{secrets.token_hex(16)}.tar.gz"
        error = await receive(request, source, settings.max_source_mb)
        if error is not None:
            source.unlink(missing_ok=True)
            return error
        result = await jobs.run(NewJob(chat.project_id, chat.id, user.id, params), source)
        return answer(result)

    @router.post("/apple/build")
    async def build(
        request: Request,
        platform: Annotated[ApplePlatform, Query()],
        action: Annotated[AppleAction, Query()] = "build",
        scheme: Annotated[str | None, Query(max_length=200)] = None,
    ) -> Response:
        return await submit(request, BuildParams(platform=platform, action=action, scheme=scheme))

    @router.post("/apple/screenshot")
    async def screenshot(
        request: Request,
        platform: Annotated[ApplePlatform, Query()],
        device: Annotated[str | None, Query(max_length=200)] = None,
        dark: Annotated[bool, Query()] = False,
    ) -> Response:
        params = ScreenshotParams(platform=platform, device=device, dark=dark)
        return await submit(request, params)

    return router


async def may_build(jobs: AppleJobs, settings: AppleSettings, user: User) -> Response | None:
    """Why this user cannot build now, or None."""
    if not settings.enabled:
        return problem(403, "Apple builds are turned off on this server",
                       "an admin can turn them on (Admin > Apple)")  # fmt: skip
    granted = user.role == "admin" or (settings.allowed == "granted" and user.apple_allowed)
    if settings.allowed != "everyone" and not granted:
        return problem(403, "you may not build Apple apps on this server",
                       "ask an admin to allow it for your account")  # fmt: skip
    limit = settings.minutes_per_month
    if limit and await jobs.minutes_used(user.id) >= limit:
        return problem(429, f"your {limit:.0f} Mac minutes of this month are used up",
                       "ask an admin for more, or wait for the next month")  # fmt: skip
    if not await any_online(jobs.db):
        hint = "an admin starts forge-mac-worker on a Mac (see the setup guide)"
        return problem(503, "no Mac is connected to this server", hint)
    return None


async def receive(request: Request, target: Path, max_mb: int) -> Response | None:
    """Store the request body (a tar.gz of the project) in `target`, within the size limit."""
    target.parent.mkdir(parents=True, exist_ok=True)
    limit, size, first = max_mb * 1024 * 1024, 0, b""
    with target.open("wb") as out:
        async for chunk in request.stream():
            size += len(chunk)
            if size > limit:
                return problem(413, f"the project is larger than {max_mb} MB packed",
                               "leave build output and large media out of the project")  # fmt: skip
            first = first or chunk[:2]
            out.write(chunk)
    if first != GZIP:
        return problem(400, "the project must be sent as a gzip-compressed tar")
    return None


def answer(result: JobResult) -> Response:
    """The job's result as the sandbox's builder expects it."""
    if not result.ok:
        return problem(502, result.error or "the Mac could not run the job", result.hint)
    if result.build is not None:
        return JSONResponse(result.build.model_dump(mode="json"))
    if result.screen is not None:
        return JSONResponse(result.screen.model_dump(mode="json"))
    return problem(502, "the Mac sent no result")
