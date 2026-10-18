"""The project's one chat over HTTP (W44): read it, change it, clear it; send messages, answer
requests, stop."""

import json
import time
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import select

from forge_sandbox.methods import ChatMode
from forge_sandbox.mux import ChannelClosed
from forge_sandbox.rpc import RpcError
from forge_web.access import RANK, project_role, require_chat, require_project
from forge_web.apple.approvals import note_answer
from forge_web.auth.sessions import CurrentUser, client_ip
from forge_web.chats.one import new_chat, project_chat
from forge_web.containers.driver import SandboxError
from forge_web.db.models import Chat, ChatEvent, Project, User
from forge_web.hosting.golive import note_go_live
from forge_web.quotas import check_disk
from forge_web.services import Services, services_of

MAX_PAGE = 500


class ChatIn(BaseModel):
    """What clients from before W44 sent to make a chat: the project's one chat is returned,
    with this mode and model."""

    title: str = Field(default="New chat", min_length=1, max_length=200)
    mode: ChatMode = "edits"
    model: str = Field(default="", max_length=200)


class ChatPatch(BaseModel):
    """Changes to a chat."""

    title: str | None = Field(default=None, min_length=1, max_length=200)
    mode: ChatMode | None = None
    model: str | None = Field(default=None, max_length=200)


class MessageIn(BaseModel):
    """A message to Forge (a prompt, or a slash command)."""

    text: str = Field(min_length=1, max_length=100_000)


class AnswerIn(BaseModel):
    """The answer to an approval or a question."""

    request_id: str = Field(min_length=1, max_length=64)
    answer: dict[str, Any]


class ChatOut(BaseModel):
    """A chat as the web UI sees it."""

    id: str
    project_id: str
    title: str
    state: str
    mode: str
    model: str
    can_write: bool  # editors and owners send, answer, change and clear
    created_at: float
    updated_at: float


def chat_out(chat: Chat, role: str) -> ChatOut:
    """The UI view of a chat for a member with this role."""
    return ChatOut(
        id=chat.id, project_id=chat.project_id, title=chat.title, state=chat.state,
        mode=chat.mode, model=chat.model, can_write=RANK[role] >= RANK["editor"],
        created_at=chat.created_at, updated_at=chat.updated_at,
    )  # fmt: skip


async def role_in(session: Any, user: User, chat: Chat) -> str:
    """The user's role in the chat's project (require_chat checked that there is one)."""
    return await project_role(session, user, chat.project_id) or "viewer"


async def stored_items(
    services: Services, chat_id: str, after: int, limit: int
) -> list[dict[str, Any]]:
    """Stored items after number `after`, oldest first."""
    await services.writer.flush()
    async with services.db.session() as session:
        rows = await session.scalars(
            select(ChatEvent)
            .where(ChatEvent.chat_id == chat_id, ChatEvent.seq > after)
            .order_by(ChatEvent.seq)
            .limit(min(limit, MAX_PAGE))
        )
        return [{"seq": row.seq, "item": json.loads(row.data)} for row in rows]


def sandbox_failure(err: Exception) -> HTTPException:
    """An HTTP error for a failed sandbox call."""
    if isinstance(err, RpcError):
        return HTTPException(409 if err.code == "busy" else 502, err.message)
    return HTTPException(503, "the project's sandbox is not reachable")


def chats_router() -> APIRouter:
    """`/api/projects/{id}/chats` and `/api/chats/...`."""
    router = APIRouter()

    @router.get("/api/projects/{project_id}/chat")
    async def get_project_chat(project_id: str, request: Request, user: CurrentUser) -> ChatOut:
        async with services_of(request).db.session() as session, session.begin():
            project, role = await require_project(session, user, project_id)
            chat = await project_chat(session, project)
        return chat_out(chat, role)

    @router.get("/api/projects/{project_id}/chats")
    async def list_chats(project_id: str, request: Request, user: CurrentUser) -> list[ChatOut]:
        return [await get_project_chat(project_id, request, user)]  # clients from before W44

    @router.post("/api/projects/{project_id}/chats", status_code=201)
    async def create_chat(
        project_id: str, body: ChatIn, request: Request, user: CurrentUser
    ) -> ChatOut:
        async with services_of(request).db.session() as session, session.begin():
            project, role = await require_project(session, user, project_id, "editor")
            chat = await project_chat(session, project)
            chat.mode, chat.model = body.mode, body.model or chat.model  # what was asked for
        services_of(request).runs.warm(project_id)  # a worker loads while the user types
        return chat_out(chat, role)

    @router.get("/api/chats/{chat_id}")
    async def get_chat(chat_id: str, request: Request, user: CurrentUser) -> dict[str, Any]:
        services = services_of(request)
        async with services.db.session() as session:
            chat, _ = await require_chat(session, user, chat_id)
            role = await role_in(session, user, chat)
        snapshot = services.runs.snapshot(chat_id) or {
            "pending": [],
            "streaming": {},
            "outputs": {},
        }
        return {**chat_out(chat, role).model_dump(), "live": snapshot}

    @router.patch("/api/chats/{chat_id}")
    async def patch_chat(
        chat_id: str, body: ChatPatch, request: Request, user: CurrentUser
    ) -> ChatOut:
        services = services_of(request)
        async with services.db.session() as session, session.begin():
            chat, _ = await require_chat(session, user, chat_id, write=True)
            role = await role_in(session, user, chat)
            for name, value in body.model_dump(exclude_none=True).items():
                setattr(chat, name, value)
            chat.updated_at = time.time()
        live = services.runs.lives.get(chat_id)
        if live is not None and body.title is not None:
            live.title = body.title
        return chat_out(chat, role)

    @router.delete("/api/chats/{chat_id}", status_code=204)
    async def clear_chat(chat_id: str, request: Request, user: CurrentUser) -> None:
        """Clear the conversation: the chat and its items go, an empty one takes its place."""
        services = services_of(request)
        async with services.db.session() as session:
            chat, _ = await require_chat(session, user, chat_id, write=True)
        await services.runs.forget_chat(chat)
        async with services.db.session() as session, session.begin():
            row = await session.get(Chat, chat_id)
            project = await session.get(Project, chat.project_id)
            if row is not None and project is not None:
                await session.delete(row)
                await session.flush()  # the project may have one chat only
                session.add(await new_chat(session, project, row.mode, row.model))

    @router.get("/api/chats/{chat_id}/events")
    async def chat_events(
        chat_id: str,
        request: Request,
        user: CurrentUser,
        after: int = 0,
        limit: int = MAX_PAGE,
    ) -> dict[str, Any]:
        services = services_of(request)
        async with services.db.session() as session:
            await require_chat(session, user, chat_id)
        items = await stored_items(services, chat_id, max(after, 0), max(limit, 1))
        return {"items": items, "last_seq": items[-1]["seq"] if items else after}

    @router.post("/api/chats/{chat_id}/messages", status_code=202)
    async def send_message(
        chat_id: str, body: MessageIn, request: Request, user: CurrentUser
    ) -> dict[str, bool]:
        services = services_of(request)
        async with services.db.session() as session, session.begin():
            chat, _ = await require_chat(session, user, chat_id, write=True)
            chat.started_by = user.id  # stopped with this member's access (members.stop_chats)
        await check_disk(services, chat.project_id)
        try:
            await services.runs.send(chat, body.text)
        except (RpcError, ChannelClosed, SandboxError, OSError, TimeoutError) as err:
            raise sandbox_failure(err) from None
        return {"ok": True}

    @router.post("/api/chats/{chat_id}/answer")
    async def answer(
        chat_id: str, body: AnswerIn, request: Request, user: CurrentUser
    ) -> dict[str, bool]:
        services = services_of(request)
        async with services.db.session() as session:
            chat, _ = await require_chat(session, user, chat_id, write=True)
        try:
            accepted = await services.runs.answer(chat, body.request_id, body.answer)
        except (RpcError, ChannelClosed, SandboxError, OSError, TimeoutError) as err:
            raise sandbox_failure(err) from None
        if accepted:  # approvals are recorded only once Forge has taken the answer
            ip = client_ip(request)
            await note_answer(services, chat, user, body.request_id, body.answer, ip)
            await note_go_live(services, chat, user, body.request_id, body.answer, ip)
        return {"accepted": accepted}

    @router.post("/api/chats/{chat_id}/cancel")
    async def cancel(chat_id: str, request: Request, user: CurrentUser) -> dict[str, bool]:
        services = services_of(request)
        async with services.db.session() as session:
            chat, _ = await require_chat(session, user, chat_id, write=True)
        try:
            await services.runs.cancel(chat)
        except (RpcError, ChannelClosed, SandboxError, OSError, TimeoutError) as err:
            raise sandbox_failure(err) from None
        return {"ok": True}

    return router
