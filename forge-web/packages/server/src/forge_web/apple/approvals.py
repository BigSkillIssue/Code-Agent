"""Approving an Apple app (W21): what the approval page shows, and the user's "Ready for Apple".

Reviews, screenshots and the approval question come from the project's sandbox and are shown as
they came; what counts is the user's own answer to Forge's approval question. An approval keeps
who gave it, when, and the commit the project was at, so the App Store step uses exactly that.
"""

import asyncio
import contextlib
import json
import re
import secrets
import time
from typing import Any

from fastapi import APIRouter, Request
from forge.apple_flow import APPROVE, NOT_YET, SEND_BACK
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from forge_sandbox.mux import ChannelClosed
from forge_sandbox.rpc import RpcError
from forge_web.apple.questions import PURPOSE, chosen
from forge_web.audit import audit
from forge_web.auth.sessions import CurrentUser
from forge_web.containers.driver import SandboxError
from forge_web.db.models import AppleApproval, AppleJob, Chat, ChatEvent, User
from forge_web.files_api import allowed
from forge_web.services import Services

STAGES = ("prompt", "plan", "product", "listing")  # the store texts after the approval (S61)
SCAN = 200  # the newest reviews and requests looked at
SHOWN = 20  # builds and approvals on the page
COMMIT = re.compile(r"^[0-9a-f]{40,64}$")
FLUSH_WAIT_S = 5.0  # for the question's item to be written, while other chats keep the writer busy


def approvals_router() -> APIRouter:
    """`/api/projects/{id}/apple/review`: everything the approval page shows but the pictures."""
    router = APIRouter(prefix="/api/projects/{project_id}/apple")

    @router.get("/review")
    async def review(project_id: str, request: Request, user: CurrentUser) -> dict[str, Any]:
        services = await allowed(request, user, project_id)
        async with services.db.session() as session:
            return {
                "reviews": await latest_reviews(session, project_id),
                "pending": await pending_approval(session, project_id),
                "choices": {"approve": APPROVE, "send_back": SEND_BACK, "not_yet": NOT_YET},
                "builds": await recent_builds(session, project_id),
                "approvals": await approvals_of(session, project_id),
            }

    return router


def project_rows(project_id: str, item_type: str) -> Any:
    """The newest stored items of one type from the project's chats."""
    return (
        select(ChatEvent.chat_id, ChatEvent.seq, ChatEvent.data)
        .join(Chat, Chat.id == ChatEvent.chat_id)
        .where(Chat.project_id == project_id, ChatEvent.type == item_type)
        .order_by(ChatEvent.ts.desc())
        .limit(SCAN)
    )


async def latest_reviews(session: AsyncSession, project_id: str) -> list[dict[str, Any]]:
    """The newest guideline review of each stage (request, plan, app)."""
    query = project_rows(project_id, "event").where(ChatEvent.kind == "guideline_review")
    latest: dict[str, dict[str, Any]] = {}
    for chat_id, _seq, data in await session.execute(query):
        event = json.loads(data).get("event", {})
        stage = event.get("stage")
        if stage in STAGES and stage not in latest:
            latest[stage] = {**event, "chat_id": chat_id}
    return [latest[stage] for stage in STAGES if stage in latest]


async def pending_approval(session: AsyncSession, project_id: str) -> dict[str, Any] | None:
    """Forge's newest approval question, while it waits for an answer."""
    for chat_id, seq, data in await session.execute(project_rows(project_id, "request")):
        item = json.loads(data)
        if item.get("purpose") != PURPOSE:
            continue
        if await answered(session, chat_id, seq, item["id"]):
            return None  # the newest approval question is answered: nothing waits
        question = item["payload"]["questions"][0]
        return {"chat_id": chat_id, "request_id": item["id"], "text": question["text"]}
    return None


async def answered(session: AsyncSession, chat_id: str, seq: int, request_id: str) -> bool:
    """Whether a request of a chat was answered (or withdrawn) after it was asked."""
    later = (
        ChatEvent.chat_id == chat_id,
        ChatEvent.seq > seq,
        ChatEvent.type == "request_resolved",
    )
    rows = await session.scalars(select(ChatEvent.data).where(*later))
    return any(json.loads(data).get("id") == request_id for data in rows)


async def recent_builds(session: AsyncSession, project_id: str) -> list[dict[str, Any]]:
    """The project's newest jobs on the Macs."""
    rows = await session.scalars(
        select(AppleJob).where(AppleJob.project_id == project_id)
        .order_by(AppleJob.created_at.desc()).limit(SHOWN)
    )  # fmt: skip
    return [
        {"id": job.id, "kind": job.kind, "params": json.loads(job.params or "{}"),
         "status": job.status, "outcome": job.outcome, "seconds": job.seconds,
         "created_at": job.created_at}
        for job in rows
    ]  # fmt: skip


async def approvals_of(session: AsyncSession, project_id: str) -> list[dict[str, Any]]:
    """The project's approvals, newest first."""
    rows = await session.execute(
        select(AppleApproval, User.name, User.email)
        .join(User, User.id == AppleApproval.user_id)
        .where(AppleApproval.project_id == project_id)
        .order_by(AppleApproval.created_at.desc()).limit(SHOWN)
    )  # fmt: skip
    return [
        {"id": row.id, "chat_id": row.chat_id, "user": name or email or row.user_id,
         "at": row.created_at, "commit": row.commit, "clean": row.clean, "summary": row.summary}
        for row, name, email in rows
    ]  # fmt: skip


async def note_answer(
    services: Services, chat: Chat, user: User, request_id: str, answer: Any, ip: str
) -> None:
    """Record an accepted answer to Forge's approval question (other answers are not looked at)."""
    choice = chosen(answer)
    if choice not in (APPROVE, SEND_BACK):
        return
    with contextlib.suppress(TimeoutError):  # the question is stored by now, or soon
        await asyncio.wait_for(services.writer.flush(), FLUSH_WAIT_S)
    async with services.db.session() as session:
        if not await is_approval_request(session, chat.id, request_id):
            return
        summary = await product_summary(session, chat.id)
    where = {"user_id": user.id, "target": chat.project_id, "ip": ip}
    if choice == SEND_BACK:
        await audit(services.db, "apple.sent_back", **where, chat=chat.id)
        return
    commit, clean = await project_state(services, chat.project_id)
    async with services.db.session() as session, session.begin():
        session.add(AppleApproval(id=secrets.token_hex(8), project_id=chat.project_id,
                                  chat_id=chat.id, user_id=user.id, request_id=request_id,
                                  commit=commit, clean=clean, summary=summary[:4000],
                                  created_at=time.time()))  # fmt: skip
    await audit(services.db, "apple.approved", **where, chat=chat.id, commit=commit, clean=clean)


async def is_approval_request(session: AsyncSession, chat_id: str, request_id: str) -> bool:
    """Whether the chat's request with this id is Forge's approval question."""
    rows = await session.scalars(
        select(ChatEvent.data).where(ChatEvent.chat_id == chat_id, ChatEvent.type == "request")
        .order_by(ChatEvent.seq.desc()).limit(SCAN)
    )  # fmt: skip
    for data in rows:
        item = json.loads(data)
        if item.get("id") == request_id:
            return bool(item.get("purpose") == PURPOSE)
    return False


async def product_summary(session: AsyncSession, chat_id: str) -> str:
    """The summary of the chat's newest review of the finished app."""
    rows = await session.scalars(
        select(ChatEvent.data)
        .where(ChatEvent.chat_id == chat_id, ChatEvent.kind == "guideline_review")
        .order_by(ChatEvent.seq.desc()).limit(SCAN)
    )  # fmt: skip
    for data in rows:
        event = json.loads(data).get("event", {})
        if event.get("stage") == "product":
            return str(event.get("summary", ""))
    return ""


async def project_state(services: Services, project_id: str) -> tuple[str, bool]:
    """The project's HEAD commit and whether nothing is uncommitted ("" when unknown)."""
    try:
        log = await services.runs.call(project_id, "git.log", {"limit": 1})
        status = await services.runs.call(project_id, "git.status", {})
    except (RpcError, ChannelClosed, SandboxError, OSError, TimeoutError):
        return "", False
    commits = log.get("commits") if isinstance(log, dict) else None
    first = commits[0] if isinstance(commits, list) and commits else {}
    commit = first.get("commit") if isinstance(first, dict) else ""
    if not isinstance(commit, str) or not COMMIT.match(commit):
        return "", False
    files = status.get("files") if isinstance(status, dict) else None
    return commit, files == []
