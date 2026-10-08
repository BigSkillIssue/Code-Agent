"""A project's files over HTTP: list, read, save, create folders, rename, delete, upload,
download, and the search behind @-mentions. Reading needs the viewer role, changing the editor
role. Downloads are always attachments, so a project's HTML never runs on this site."""

import base64
import contextlib
import secrets
from collections.abc import AsyncIterator
from typing import Any
from urllib.parse import quote

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from forge_web.access import require_project
from forge_web.auth.sessions import CurrentUser
from forge_web.sandbox_calls import result_dict, sandbox_call
from forge_web.services import Services, services_of

PART = 512 * 1024  # bytes per sandbox message when files move in parts
MAX_PATH = 4096


class TextFileIn(BaseModel):
    """A text file saved from the editor; `expected_mtime` refuses to overwrite newer changes."""

    path: str = Field(min_length=1, max_length=MAX_PATH)
    text: str = Field(max_length=5_000_000)
    expected_mtime: float | None = None


class PathIn(BaseModel):
    """One path in the project."""

    path: str = Field(min_length=1, max_length=MAX_PATH)


class RenameIn(BaseModel):
    """Move or rename."""

    src: str = Field(min_length=1, max_length=MAX_PATH)
    dst: str = Field(min_length=1, max_length=MAX_PATH)


async def allowed(request: Request, user: Any, project_id: str, need: str = "viewer") -> Services:
    """The services, once the user may `need` in the project (404/403 otherwise)."""
    services = services_of(request)
    async with services.db.session() as session:
        await require_project(session, user, project_id, need)
    return services


def files_router() -> APIRouter:
    """`/api/projects/{id}/files/...`."""
    router = APIRouter(prefix="/api/projects/{project_id}/files")
    reading_routes(router)
    changing_routes(router)
    transfer_routes(router)
    return router


def reading_routes(router: APIRouter) -> None:
    """List, read and search."""

    @router.get("/list")
    async def listing(
        project_id: str,
        request: Request,
        user: CurrentUser,
        path: str = Query("", max_length=MAX_PATH),
    ) -> dict[str, Any]:
        services = await allowed(request, user, project_id)
        found = result_dict(await sandbox_call(services, project_id, "fs.list", {"path": path}))
        entries = found.get("entries")
        return {"path": path, "entries": entries if isinstance(entries, list) else []}

    @router.get("/content")
    async def content(
        project_id: str,
        request: Request,
        user: CurrentUser,
        path: str = Query("", max_length=MAX_PATH),
    ) -> dict[str, Any]:
        services = await allowed(request, user, project_id)
        if not path:
            raise HTTPException(400, "which file?")
        read = result_dict(await sandbox_call(services, project_id, "fs.read", {"path": path}))
        binary = bool(read.get("binary"))
        return {
            "path": path, "size": read.get("size"), "mtime": read.get("mtime"),
            "truncated": bool(read.get("truncated")), "binary": binary,
            "text": None if binary else read.get("text"),
        }  # fmt: skip

    @router.get("/search")
    async def search(
        project_id: str,
        request: Request,
        user: CurrentUser,
        query: str = Query("", max_length=200),
        limit: int = Query(50, ge=1, le=200),
    ) -> dict[str, Any]:
        services = await allowed(request, user, project_id)
        params = {"query": query, "limit": limit}
        found = result_dict(await sandbox_call(services, project_id, "git.files", params))
        files = found.get("files")
        files = [f for f in files if isinstance(f, str)][:limit] if isinstance(files, list) else []
        total = found.get("total")
        return {"files": files, "total": total if isinstance(total, int) else len(files)}


def changing_routes(router: APIRouter) -> None:
    """Save, create folders, rename, delete."""

    @router.put("/content")
    async def save(project_id: str, body: TextFileIn, request: Request, user: CurrentUser) -> Any:
        services = await allowed(request, user, project_id, "editor")
        params = {"path": body.path, "text": body.text, "create_dirs": True,
                  "expected_mtime": body.expected_mtime}  # fmt: skip
        return result_dict(await sandbox_call(services, project_id, "fs.write", params))

    @router.post("/mkdir")
    async def mkdir(project_id: str, body: PathIn, request: Request, user: CurrentUser) -> Any:
        services = await allowed(request, user, project_id, "editor")
        return result_dict(
            await sandbox_call(services, project_id, "fs.mkdir", {"path": body.path})
        )

    @router.post("/rename")
    async def rename(project_id: str, body: RenameIn, request: Request, user: CurrentUser) -> Any:
        services = await allowed(request, user, project_id, "editor")
        params = {"src": body.src, "dst": body.dst}
        return result_dict(await sandbox_call(services, project_id, "fs.rename", params))

    @router.delete("", status_code=204)
    async def delete(
        project_id: str,
        request: Request,
        user: CurrentUser,
        path: str = Query("", max_length=MAX_PATH),
        recursive: bool = False,
    ) -> None:
        services = await allowed(request, user, project_id, "editor")
        if not path:
            raise HTTPException(400, "which file?")
        await sandbox_call(
            services, project_id, "fs.delete", {"path": path, "recursive": recursive}
        )


def transfer_routes(router: APIRouter) -> None:
    """Upload and download files of any size, in parts."""

    @router.put("/raw")
    async def upload(
        project_id: str,
        request: Request,
        user: CurrentUser,
        path: str = Query("", max_length=MAX_PATH),
    ) -> Any:
        services = await allowed(request, user, project_id, "editor")
        if not path:
            raise HTTPException(400, "which file?")
        limit = services.settings.server.max_upload_mb * 1024 * 1024
        return await receive(services, project_id, path, request.stream(), limit)

    @router.get("/raw")
    async def download(
        project_id: str,
        request: Request,
        user: CurrentUser,
        path: str = Query("", max_length=MAX_PATH),
    ) -> StreamingResponse:
        services = await allowed(request, user, project_id)
        if not path:
            raise HTTPException(400, "which file?")
        first = await read_part(services, project_id, path, 0)
        name = path.rsplit("/", 1)[-1] or "download"
        headers = {
            "Content-Disposition": f"attachment; filename*=UTF-8''{quote(name)}",
            "Content-Security-Policy": "sandbox", "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
        }  # fmt: skip
        return StreamingResponse(
            send(services, project_id, path, first), media_type="application/octet-stream",
            headers=headers,
        )  # fmt: skip


async def receive(
    services: Services, project_id: str, path: str, stream: AsyncIterator[bytes], limit: int
) -> Any:
    """Write a request body into the project in parts; the file appears only when complete."""
    upload, buffer, total = secrets.token_hex(8), bytearray(), 0
    try:
        async for chunk in stream:
            buffer += chunk
            total += len(chunk)
            if total > limit:
                raise HTTPException(413, f"files may have at most {limit // (1024 * 1024)} MB")
            while len(buffer) >= PART:
                await write_part(services, project_id, path, upload, bytes(buffer[:PART]), False)
                del buffer[:PART]
        return await write_part(services, project_id, path, upload, bytes(buffer), True)
    except BaseException:
        params = {"path": path, "upload": upload, "abort": True}
        with contextlib.suppress(HTTPException):  # the sandbox may be what failed
            await sandbox_call(services, project_id, "fs.write_part", params)
        raise


async def write_part(
    services: Services, project_id: str, path: str, upload: str, data: bytes, last: bool
) -> dict[str, Any]:
    """Send one part of an upload."""
    params = {"path": path, "upload": upload, "base64": base64.b64encode(data).decode(),
              "last": last, "create_dirs": True}  # fmt: skip
    return result_dict(await sandbox_call(services, project_id, "fs.write_part", params))


async def read_part(services: Services, project_id: str, path: str, offset: int) -> dict[str, Any]:
    """One part of a file, from `offset`."""
    params = {"path": path, "offset": offset, "limit": PART}
    return result_dict(await sandbox_call(services, project_id, "fs.read", params))


async def send(
    services: Services, project_id: str, path: str, first: dict[str, Any]
) -> AsyncIterator[bytes]:
    """A file's bytes, part by part."""
    part, offset = first, 0
    while True:
        data = part_bytes(part)
        yield data
        offset += len(data)
        if not part.get("truncated") or not data:
            return
        part = await read_part(services, project_id, path, offset)


def part_bytes(part: dict[str, Any]) -> bytes:
    """The bytes of one read result (text or base64)."""
    if isinstance(part.get("base64"), str):
        return base64.b64decode(part["base64"])
    return str(part.get("text") or "").encode("utf-8")
