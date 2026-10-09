"""Who is watching what: browser connections subscribed to chats and to their own user feed.

A subscriber that subscribes to a chat first holds back live messages for it, replays the stored
items after the number it already has, then lets through the held-back messages it did not just
replay — so a reconnecting tab sees every item once and in order.
"""

import asyncio
import json
from collections import defaultdict
from typing import Any

# How far a slow browser may fall behind before it is disconnected (it reconnects and catches up
# from the last item it has). Bytes count, not only messages: one item can be large.
MAX_QUEUED = 20_000
MAX_QUEUED_BYTES = 32 * 1024 * 1024


def encode(message: dict[str, Any]) -> str:
    """A message as the JSON text sent to the browser."""
    return json.dumps(message, ensure_ascii=False, separators=(",", ":"))


class Subscriber:
    """One browser connection's outgoing messages."""

    def __init__(self, user_id: str) -> None:
        self.user_id = user_id
        self.queue: asyncio.Queue[str | None] = asyncio.Queue()
        self.chats: set[str] = set()
        self.queued_bytes = 0
        self.held_bytes = 0
        self._held: dict[str, list[tuple[dict[str, Any], str]]] = {}
        self._room = asyncio.Event()
        self._room.set()
        self.overflowed = False

    def put(self, message: dict[str, Any]) -> None:
        """Queue a message (or hold it while its chat is being replayed)."""
        text = encode(message)
        held = self._held.get(str(message.get("chat_id", "")))
        if held is None:
            self._enqueue(text)
        elif self._fits(len(text)):
            held.append((message, text))
            self.held_bytes += len(text)

    def put_now(self, message: dict[str, Any]) -> None:
        """Queue a message past any hold (the replay itself)."""
        self._enqueue(encode(message))

    def _fits(self, size: int) -> bool:
        if self.overflowed:
            return False
        if self.queue.qsize() < MAX_QUEUED and self.queued_bytes + self.held_bytes + size <= (
            MAX_QUEUED_BYTES
        ):
            return True
        self.overflowed = True
        self.queue.put_nowait(None)  # the connection closes; the browser reconnects
        return False

    def _enqueue(self, text: str) -> None:
        if not self._fits(len(text)):
            return
        self.queued_bytes += len(text)
        self.queue.put_nowait(text)
        if self.queued_bytes > MAX_QUEUED_BYTES // 2:
            self._room.clear()

    async def next(self) -> str | None:
        """The next message to send; None when the connection should close."""
        text = await self.queue.get()
        if text is not None:
            self.queued_bytes -= len(text)
            if self.queued_bytes <= MAX_QUEUED_BYTES // 2:
                self._room.set()
        return text

    async def room(self) -> None:
        """Wait until the browser has read enough for a replay to go on."""
        await self._room.wait()

    def hold(self, chat_id: str) -> None:
        """Start holding back live messages of a chat."""
        self._held[chat_id] = []

    def release(self, chat_id: str, replayed_up_to: int) -> None:
        """Let held messages through, except stored items the replay already sent."""
        for message, text in self._held.pop(chat_id, []):
            self.held_bytes -= len(text)
            if message.get("type") == "item" and int(message.get("seq", 0)) <= replayed_up_to:
                continue
            self._enqueue(text)

    def close(self) -> None:
        """End the outgoing stream."""
        self.queue.put_nowait(None)


class Hub:
    """Routes chat and user messages to the subscribers that asked for them."""

    def __init__(self) -> None:
        self._chats: dict[str, set[Subscriber]] = defaultdict(set)
        self._users: dict[str, set[Subscriber]] = defaultdict(set)

    def join(self, subscriber: Subscriber) -> None:
        """Receive messages for the subscriber's user."""
        self._users[subscriber.user_id].add(subscriber)

    def leave(self, subscriber: Subscriber) -> None:
        """Receive nothing more."""
        self._users[subscriber.user_id].discard(subscriber)
        for chat_id in list(subscriber.chats):
            self.unwatch(subscriber, chat_id)

    def watch(self, subscriber: Subscriber, chat_id: str) -> None:
        """Receive a chat's items."""
        subscriber.chats.add(chat_id)
        self._chats[chat_id].add(subscriber)

    def unwatch(self, subscriber: Subscriber, chat_id: str) -> None:
        """Stop receiving a chat's items."""
        subscriber.chats.discard(chat_id)
        watchers = self._chats.get(chat_id)
        if watchers is not None:
            watchers.discard(subscriber)
            if not watchers:
                del self._chats[chat_id]

    def watching(self, chat_id: str) -> int:
        """How many connections watch a chat."""
        return len(self._chats.get(chat_id, ()))

    def to_chat(self, chat_id: str, message: dict[str, Any]) -> None:
        """Send to everyone watching the chat."""
        for subscriber in list(self._chats.get(chat_id, ())):
            subscriber.put(message)

    def to_users(self, user_ids: set[str], message: dict[str, Any]) -> None:
        """Send to every connection of these users (sidebar updates)."""
        for user_id in user_ids:
            for subscriber in list(self._users.get(user_id, ())):
                subscriber.put(message)
