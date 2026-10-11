"""Forge's final question in an app chat, "Ready to go live?" (`forge.app_flow`), and the user's
answer to it: only "Ready to go live" is recorded, with who gave it, when, and the commit the
project was at. A deploy can only start from such a commit (S67b's `Report.ready_to_host`)."""

import asyncio
import contextlib
import json
import secrets
import time
from typing import Any

from forge.app_flow import GO_LIVE
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from forge_web.apple.approvals import FLUSH_WAIT_S, SCAN, project_state
from forge_web.apple.questions import chosen
from forge_web.audit import audit
from forge_web.db.hosting_models import GoLiveApproval
from forge_web.db.models import Chat, ChatEvent, User
from forge_web.hosting.questions import PURPOSE
from forge_web.services import Services


async def is_go_live_request(session: AsyncSession, chat_id: str, request_id: str) -> bool:
    """Whether the chat's request with this id is Forge's go-live question."""
    rows = await session.scalars(
        select(ChatEvent.data).where(ChatEvent.chat_id == chat_id, ChatEvent.type == "request")
        .order_by(ChatEvent.seq.desc()).limit(SCAN)
    )  # fmt: skip
    for data in rows:
        item = json.loads(data)
        if item.get("id") == request_id:
            return bool(item.get("purpose") == PURPOSE)
    return False


async def release_summary(session: AsyncSession, chat_id: str) -> str:
    """The summary of the chat's newest release review of the finished product."""
    rows = await session.scalars(
        select(ChatEvent.data)
        .where(ChatEvent.chat_id == chat_id, ChatEvent.kind == "release_review")
        .order_by(ChatEvent.seq.desc()).limit(SCAN)
    )  # fmt: skip
    for data in rows:
        event = json.loads(data).get("event", {})
        if event.get("stage") == "product":
            return str(event.get("summary", ""))
    return ""


async def note_go_live(
    services: Services, chat: Chat, user: User, request_id: str, answer: Any, ip: str
) -> None:
    """Record an accepted "Ready to go live" (other answers are not looked at)."""
    if chosen(answer) != GO_LIVE:
        return
    with contextlib.suppress(TimeoutError):  # the question is stored by now, or soon
        await asyncio.wait_for(services.writer.flush(), FLUSH_WAIT_S)
    async with services.db.session() as session:
        if not await is_go_live_request(session, chat.id, request_id):
            return
        summary = await release_summary(session, chat.id)
    commit, clean = await project_state(services, chat.project_id)
    async with services.db.session() as session, session.begin():
        session.add(GoLiveApproval(id=secrets.token_hex(8), project_id=chat.project_id,
                                   chat_id=chat.id, user_id=user.id, request_id=request_id,
                                   commit=commit, clean=clean, summary=summary[:4000],
                                   created_at=time.time()))  # fmt: skip
    await audit(services.db, "hosting.go_live", user_id=user.id, target=chat.project_id, ip=ip,
                chat=chat.id, commit=commit, clean=clean)  # fmt: skip
