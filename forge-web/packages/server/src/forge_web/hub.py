"""Who is watching what: browser connections subscribed to chats and to their own user feed.

A subscriber that subscribes to a chat first holds back live messages for it, replays the stored
items after the number it already has, then lets through the held-back messages it did not just
replay — so a reconnecting tab sees every item once and in order.
"""

import asyncio
from collections import defaultdict
from typing import Any

MAX_QUEUED = 20_000  # messages a slow browser may fall behind before it is disconnected


class Subscriber:
    """One browser connection's outgoing messages."""

    def __init__(self, user_id: str) -> None:
        self.user_id = user_id
        self.queue: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()
        self.chats: set[str] = set()
        self._held: dict[str, list[dict[str, Any]]] = {}
        self.overflowed = False

    def put(self, message: dict[str, Any]) -> None:
        """Queue a message (or hold it while its chat is being replayed)."""
        held = self._held.get(str(message.get("chat_id", "")))
        if held is not None:
            held.append(message)
            return
        if self.queue.qsize() >= MAX_QUEUED:
            self.overflowed = True
            self.queue.put_nowait(None)  # the connection closes; the browser reconnects
            return
        self.queue.put_nowait(message)

    def hold(self, chat_id: str) -> None:
        """Start holding back live messages of a chat."""
        self._held[chat_id] = []

    def release(self, chat_id: str, replayed_up_to: int) -> None:
        """Let held messages through, except stored items the replay already sent."""
        for message in self._held.pop(chat_id, []):
            if message.get("type") == "item" and int(message.get("seq", 0)) <= replayed_up_to:
                continue
            self.put(message)

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
