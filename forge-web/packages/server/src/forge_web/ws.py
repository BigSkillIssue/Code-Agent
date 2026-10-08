"""`/api/ws`: one WebSocket per browser tab for live chat items and sidebar updates.

The browser sends `{"type": "subscribe", "chat_id", "after_seq"}` and gets every stored item
after that number, a `subscribed` message with the live-only state, then live messages:
`item` (stored, numbered), `live` (streaming text and command output, not numbered) and
`chat_state` (a chat's state or title changed).
"""

import asyncio
import contextlib
from typing import Any

from fastapi import APIRouter, HTTPException, WebSocket, WebSocketDisconnect

from forge_web.access import require_chat
from forge_web.auth.sessions import websocket_user
from forge_web.chats.api import stored_items
from forge_web.db.models import User
from forge_web.hub import Subscriber
from forge_web.services import Services, services_of

MAX_SUBSCRIPTIONS = 50
REPLAY_PAGE = 5000


def ws_router() -> APIRouter:
    """The WebSocket route."""
    router = APIRouter()

    @router.websocket("/api/ws")
    async def socket(websocket: WebSocket) -> None:
        user = await websocket_user(websocket)
        if user is None:
            await websocket.close(code=4401)
            return
        await websocket.accept()
        await serve(websocket, services_of(websocket), user)

    return router


async def serve(websocket: WebSocket, services: Services, user: User) -> None:
    """Relay messages to the browser and handle its subscriptions until it disconnects."""
    subscriber = Subscriber(user.id)
    services.hub.join(subscriber)
    sender = asyncio.create_task(_send_all(websocket, subscriber))
    subscriber.put({"type": "hello", "user": {"id": user.id, "name": user.name}})
    try:
        while True:
            message = await websocket.receive_json()
            if isinstance(message, dict):
                await _handle(message, services, user, subscriber)
    except (WebSocketDisconnect, ValueError, RuntimeError):
        pass
    finally:
        services.hub.leave(subscriber)
        subscriber.close()
        sender.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await sender


async def _send_all(websocket: WebSocket, subscriber: Subscriber) -> None:
    while (message := await subscriber.queue.get()) is not None:
        await websocket.send_json(message)
    with contextlib.suppress(RuntimeError):
        await websocket.close(code=4408 if subscriber.overflowed else 1000)


async def _handle(
    message: dict[str, Any], services: Services, user: User, subscriber: Subscriber
) -> None:
    kind = message.get("type")
    chat_id = message.get("chat_id")
    if kind == "ping":
        subscriber.put({"type": "pong"})
    elif kind == "subscribe" and isinstance(chat_id, str):
        after = message.get("after_seq", 0)
        await subscribe(services, user, subscriber, chat_id, after if isinstance(after, int) else 0)
    elif kind == "unsubscribe" and isinstance(chat_id, str):
        services.hub.unwatch(subscriber, chat_id)


async def subscribe(
    services: Services, user: User, subscriber: Subscriber, chat_id: str, after_seq: int
) -> None:
    """Watch a chat: stored items after `after_seq`, then live ones, none twice or missing."""
    try:
        async with services.db.session() as session:
            await require_chat(session, user, chat_id)
    except HTTPException:
        subscriber.put({"type": "error", "chat_id": chat_id, "message": "no such chat"})
        return
    if len(subscriber.chats) >= MAX_SUBSCRIPTIONS and chat_id not in subscriber.chats:
        subscriber.put({"type": "error", "chat_id": chat_id, "message": "too many subscriptions"})
        return
    subscriber.hold(chat_id)  # live items wait until the replay is out
    services.hub.watch(subscriber, chat_id)
    last = max(after_seq, 0)
    while True:
        page = await stored_items(services, chat_id, last, REPLAY_PAGE)
        for entry in page:
            subscriber.queue.put_nowait({"type": "item", "chat_id": chat_id, **entry})
        if page:
            last = page[-1]["seq"]
        if len(page) < REPLAY_PAGE:
            break
    snapshot = services.runs.snapshot(chat_id) or {"pending": [], "streaming": {}, "outputs": {}}
    subscriber.queue.put_nowait(
        {"type": "subscribed", "chat_id": chat_id, "last_seq": last, "live": snapshot}
    )
    subscriber.release(chat_id, last)
