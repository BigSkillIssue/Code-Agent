"""The latest screenshot of each device of an Apple project (`.forge/out/apple/*.png`, written by
Forge's Apple checks) for the web UI, as data URLs.

Only PNG files are passed on, and only so many bytes: the files come from the sandbox.
"""

import base64
import re
from typing import Any

from fastapi import APIRouter, HTTPException, Request

from forge_web.auth.sessions import CurrentUser
from forge_web.files_api import allowed, part_bytes, read_part
from forge_web.sandbox_calls import result_dict, sandbox_call
from forge_web.services import Services

FOLDER = ".forge/out/apple"
NAME = re.compile(r"^(ios|ipados|macos|watchos)-(light|dark)\.png$")
PNG = b"\x89PNG\r\n\x1a\n"
MAX_BYTES = 12 * 1024 * 1024  # one screenshot


def screens_router() -> APIRouter:
    """`/api/projects/{id}/apple/screens`."""
    router = APIRouter(prefix="/api/projects/{project_id}/apple")

    @router.get("/screens")
    async def screens(project_id: str, request: Request, user: CurrentUser) -> list[dict[str, Any]]:
        services = await allowed(request, user, project_id)
        try:
            listing = result_dict(await sandbox_call(services, project_id, "fs.list",
                                                     {"path": FOLDER}))  # fmt: skip
        except HTTPException as err:
            if err.status_code == 404:
                return []  # no screenshots yet
            raise
        names = sorted(e["name"] for e in listing.get("entries", [])
                       if isinstance(e, dict) and NAME.match(str(e.get("name", ""))))  # fmt: skip
        found = []
        for name in names:
            data = await whole_file(services, project_id, f"{FOLDER}/{name}")
            if data is not None:
                platform, mode = NAME.match(name).groups()  # type: ignore[union-attr]
                url = "data:image/png;base64," + base64.b64encode(data).decode()
                found.append({"platform": platform, "dark": mode == "dark", "url": url})
        return found

    return router


async def whole_file(services: Services, project_id: str, path: str) -> bytes | None:
    """A screenshot's bytes, or None when it is no PNG or too large."""
    data, offset = b"", 0
    while True:
        part = await read_part(services, project_id, path, offset)
        chunk = part_bytes(part)
        data += chunk
        offset += len(chunk)
        if len(data) > MAX_BYTES or not data.startswith(PNG[: len(data)]):
            return None
        if not part.get("truncated") or not chunk:
            return data if data.startswith(PNG) else None
