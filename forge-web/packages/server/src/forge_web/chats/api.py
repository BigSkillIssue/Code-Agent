"""Chats over HTTP: create, list, change, delete; send messages, answer requests, stop."""

import json
import secrets
import time
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import or_, select

from forge_sandbox.methods import ChatMode
from forge_sandbox.mux import ChannelClosed
from forge_sandbox.rpc import RpcError
from forge_web.access import require_chat, require_project
from forge_web.auth.sessions import CurrentUser
from forge_web.containers.driver import SandboxError
from forge_web.db.models import Chat, ChatEvent, User
from forge_web.quotas import check_disk
from forge_web.services import Services, services_of

MAX_PAGE = 5000


class ChatIn(BaseModel):
    """A new chat."""

    title: str = Field(default="New chat", min_length=1, max_length=200)
    mode: ChatMode = "edits"
    model: str = Field(default="", max_length=200)


class ChatPatch(BaseModel):
    """Changes to a chat."""

    title: str | None = Field(default=None, min_length=1, max_length=200)
    mode: ChatMode | None = None
    model: str | None = Field(default=None, max_length=200)
    shared: bool | None = None


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
    shared: bool
    mine: bool
    created_at: float
    updated_at: float


def chat_out(chat: Chat, user: User) -> ChatOut:
    """The UI view of a chat."""
    return ChatOut(
        id=chat.id, project_id=chat.project_id, title=chat.title, state=chat.state,
        mode=chat.mode, model=chat.model, shared=chat.shared, mine=chat.user_id == user.id,
        created_at=chat.created_at, updated_at=chat.updated_at,
    )  # fmt: skip


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

    @router.get("/api/projects/{project_id}/chats")
    async def list_chats(project_id: str, request: Request, user: CurrentUser) -> list[ChatOut]:
        async with services_of(request).db.session() as session:
            await require_project(session, user, project_id)
            chats = await session.scalars(
                select(Chat)
                .where(Chat.project_id == project_id, or_(Chat.user_id == user.id, Chat.shared))
                .order_by(Chat.updated_at.desc())
            )
            return [chat_out(chat, user) for chat in chats]

    @router.post("/api/projects/{project_id}/chats", status_code=201)
    async def create_chat(
        project_id: str, body: ChatIn, request: Request, user: CurrentUser
    ) -> ChatOut:
        now = time.time()
        async with services_of(request).db.session() as session, session.begin():
            await require_project(session, user, project_id, "editor")
            chat = Chat(
                id=secrets.token_hex(16), project_id=project_id, user_id=user.id,
                title=body.title, mode=body.mode, model=body.model or user.default_model,
                state="idle",
                created_at=now, updated_at=now,
            )  # fmt: skip
            session.add(chat)
        services_of(request).runs.warm(project_id)  # a worker loads while the user types
        return chat_out(chat, user)

    @router.get("/api/chats/{chat_id}")
    async def get_chat(chat_id: str, request: Request, user: CurrentUser) -> dict[str, Any]:
        services = services_of(request)
        async with services.db.session() as session:
            chat, _ = await require_chat(session, user, chat_id)
        snapshot = services.runs.snapshot(chat_id) or {
            "pending": [],
            "streaming": {},
            "outputs": {},
        }
        return {**chat_out(chat, user).model_dump(), "live": snapshot}

    @router.patch("/api/chats/{chat_id}")
    async def patch_chat(
        chat_id: str, body: ChatPatch, request: Request, user: CurrentUser
    ) -> ChatOut:
        services = services_of(request)
        async with services.db.session() as session, session.begin():
            chat, _ = await require_chat(session, user, chat_id, write=True)
            for name, value in body.model_dump(exclude_none=True).items():
                setattr(chat, name, value)
            chat.updated_at = time.time()
        live = services.runs.lives.get(chat_id)
        if live is not None and body.title is not None:
            live.title = body.title
        return chat_out(chat, user)

    @router.delete("/api/chats/{chat_id}", status_code=204)
    async def delete_chat(chat_id: str, request: Request, user: CurrentUser) -> None:
        services = services_of(request)
        async with services.db.session() as session:
            chat, _ = await require_chat(session, user, chat_id, write=True)
        await services.runs.forget_chat(chat)
        async with services.db.session() as session, session.begin():
            row = await session.get(Chat, chat_id)
            if row is not None:
                await session.delete(row)

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
        async with services.db.session() as session:
            chat, _ = await require_chat(session, user, chat_id, write=True)
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
