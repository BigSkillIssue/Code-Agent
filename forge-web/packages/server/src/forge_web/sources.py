"""Where a project's files come from besides an empty start: a folder on the server (admins
only, under `sandbox.folder_roots`) or a ZIP upload, unpacked inside the project's sandbox."""

import contextlib
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request

from forge_web.auth.sessions import CurrentUser
from forge_web.containers.docker import MOUNT_UNSAFE
from forge_web.files_api import allowed, receive
from forge_web.quotas import MB, disk_limit, disk_room, disk_use
from forge_web.sandbox_calls import result_dict, sandbox_call
from forge_web.services import Services
from forge_web.settings import WebSettings

IMPORT_ARCHIVE = ".forge-web-import.zip"


def server_folder(settings: WebSettings, raw: str) -> Path:
    """A folder admins may open as a project: absolute, existing, under an allowed root, and
    neither inside nor around Forge Web's own data folder. 403/422 otherwise."""
    roots = [Path(r).expanduser().resolve() for r in settings.sandbox.folder_roots]
    if not roots:
        raise HTTPException(403, "this server does not open server folders as projects")
    path = Path(raw).expanduser()
    if not path.is_absolute():
        raise HTTPException(422, "the folder must be an absolute path")
    try:
        resolved = path.resolve(strict=True)
    except OSError:
        raise HTTPException(422, "the folder does not exist") from None
    if not resolved.is_dir():
        raise HTTPException(422, "that is not a folder")
    if MOUNT_UNSAFE & set(str(resolved)):  # docker's --mount value is comma-separated
        raise HTTPException(422, "the folder's path may not contain commas or quotes")
    data = settings.data_dir.expanduser().resolve()
    if resolved == data or data in resolved.parents or resolved in data.parents:
        raise HTTPException(403, "Forge Web's own data folder cannot be a project")
    if not any(resolved == root or root in resolved.parents for root in roots):
        raise HTTPException(403, "the folder is not under one of the allowed folders")
    return resolved


def sources_router() -> APIRouter:
    """`/api/projects/{id}/import/zip` and `/api/projects/{id}/usage`."""
    router = APIRouter(prefix="/api/projects/{project_id}")

    @router.put("/import/zip")
    async def import_zip(
        project_id: str, request: Request, user: CurrentUser, strip_root: bool = Query(True)
    ) -> dict[str, Any]:
        services = await allowed(request, user, project_id, "editor")
        room = await disk_room(services, project_id)
        upload_limit = services.settings.server.max_upload_mb * MB
        limit = upload_limit if room is None else min(upload_limit, room)
        await receive(services, project_id, IMPORT_ARCHIVE, request.stream(), limit)
        return await unpack(services, project_id, strip_root, room)

    @router.get("/usage")
    async def usage(project_id: str, request: Request, user: CurrentUser) -> dict[str, Any]:
        services = await allowed(request, user, project_id)
        return {"bytes": await disk_use(services, project_id), "limit": disk_limit(services)}

    return router


async def unpack(
    services: Services, project_id: str, strip_root: bool, room: int | None
) -> dict[str, Any]:
    """Unpack the uploaded archive (at most `room` bytes), then remove it."""
    params: dict[str, Any] = {"path": IMPORT_ARCHIVE, "strip_root": strip_root}
    if room is not None:
        params["max_bytes"] = room
    try:
        return result_dict(await sandbox_call(services, project_id, "fs.unzip", params))
    finally:
        with contextlib.suppress(HTTPException):
            await sandbox_call(services, project_id, "fs.delete", {"path": IMPORT_ARCHIVE})
        services.disk_use.pop(project_id, None)
