"""Files for a chat message (W45): an editor uploads a picture or any file of up to 20 MB, it
lands in the project's workspace under `.forge/attachments/` (kept out of commits, counted
against the disk quota), and the message names it so Forge sees it."""

import re
import secrets
import time

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel

from forge_web.access import require_chat
from forge_web.auth.sessions import CurrentUser
from forge_web.files_api import receive
from forge_web.quotas import disk_room
from forge_web.services import services_of

MAX_MB = 20  # the user's answer: pictures and every other kind of file, up to 20 MB
FOLDER = ".forge/attachments"
UNSAFE = re.compile(r"[^A-Za-z0-9._-]+")


class AttachmentOut(BaseModel):
    """An uploaded file, as the message that goes with it names it."""

    path: str
    name: str
    media_type: str
    size: int


def stored_name(name: str) -> str:
    """The file's name in the workspace: dated, unique and plain (letters, digits, . _ -)."""
    plain = UNSAFE.sub("_", name).strip("._")[-80:] or "file"
    return f"{time.strftime('%Y%m%d')}-{secrets.token_hex(4)}-{plain}"


def attachments_router() -> APIRouter:
    """`POST /api/chats/{chat_id}/attachments?name=…` with the file as the body."""
    router = APIRouter()

    @router.post("/api/chats/{chat_id}/attachments", status_code=201)
    async def upload(
        chat_id: str,
        request: Request,
        user: CurrentUser,
        name: str = Query("", max_length=255),
    ) -> AttachmentOut:
        services = services_of(request)
        async with services.db.session() as session:
            chat, _ = await require_chat(session, user, chat_id, write=True)
        if not name.strip():
            raise HTTPException(400, "which file?")  # after the access check: 403 comes first
        limit = MAX_MB * 1024 * 1024
        room = await disk_room(services, chat.project_id)
        if room is not None and room < limit:
            if room <= 0:
                raise HTTPException(413, "the project's storage is full")
            limit = room
        path = f"{FOLDER}/{stored_name(name)}"
        try:
            written = await receive(services, chat.project_id, path, request.stream(), limit)
        finally:
            services.disk_use.pop(chat.project_id, None)
        size = written.get("size") if isinstance(written, dict) else None
        kind = request.headers.get("content-type", "").split(";")[0].strip()[:127]
        return AttachmentOut(path=path, name=name, media_type=kind, size=int(size or 0))

    return router
