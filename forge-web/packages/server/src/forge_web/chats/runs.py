"""Running chats: one sandbox connection per project, one relay per chat that is open there.

A relay follows the chat's channel from the last daemon number it stored, checks every item,
stores the lasting ones under the chat's own gapless numbers and passes everything on to the
browsers watching the chat. The chat's state (idle, running, waiting) follows from the items.
"""

import asyncio
import contextlib
import json
import logging
import time
from collections import defaultdict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import func, select, update

from forge_sandbox.frames import ProtocolError
from forge_sandbox.mux import ChannelClosed, OpenFailed
from forge_sandbox.rpc import RpcError
from forge_web.chats.items import check_item, event_kind, is_live_only
from forge_web.containers.driver import ContainerDriver
from forge_web.db.engine import Database
from forge_web.db.models import Chat, ChatEvent
from forge_web.db.writer import EventRow, EventWriter
from forge_web.hub import Hub
from forge_web.sandbox_client import ForwardTarget, SandboxClient

log = logging.getLogger(__name__)
MAX_STREAMING = 200_000
MAX_OUTPUT_LINES = 500
ChatOptionsFor = Callable[[Chat, dict[str, Any]], dict[str, Any]]  # chat, link info -> options
ChatEnvFor = Callable[[Chat], dict[str, str]]
TargetsFor = Callable[[str], dict[str, ForwardTarget]]
OnLink = Callable[[str, SandboxClient], Awaitable[dict[str, Any]]]


async def _no_setup(_project_id: str, _client: SandboxClient) -> dict[str, Any]:
    return {}


@dataclass
class LiveChat:
    """What the server knows about a chat while it is open in its sandbox."""

    chat_id: str
    project_id: str
    user_id: str
    next_seq: int
    dseq: int
    boot: str
    state: str
    title: str
    streaming: dict[str, str] = field(default_factory=dict)  # agent id -> text so far
    outputs: dict[str, list[str]] = field(default_factory=dict)  # tool call id -> lines so far
    pending: dict[str, dict[str, Any]] = field(default_factory=dict)  # open requests
    relay: asyncio.Task[None] | None = None
    opened_with: str = ""  # the options of the last chat.open, as JSON

    def snapshot(self) -> dict[str, Any]:
        """The parts a browser needs that are not in the stored log."""
        return {
            "state": self.state,
            "streaming": dict(self.streaming),
            "outputs": {k: list(v) for k, v in self.outputs.items()},
            "pending": list(self.pending.values()),
        }


def next_state(live: LiveChat, item: dict[str, Any]) -> str:
    """The chat's state after a stored item."""
    kind = item["type"]
    if kind in ("user", "request_resolved") or (kind == "status" and item["state"] == "running"):
        return "waiting" if live.pending else "running"
    if kind == "request":
        return "waiting"
    if kind in ("turn", "worker_exited") or (kind == "status" and item["state"] == "idle"):
        return "idle"
    return live.state


class RunManager:
    """Connects chats to their sandboxes and keeps their logs."""

    def __init__(
        self,
        db: Database,
        writer: EventWriter,
        driver: ContainerDriver,
        hub: Hub,
        options_for: ChatOptionsFor,
        targets_for: TargetsFor | None = None,
        *,
        env_for: ChatEnvFor | None = None,
        on_link: OnLink = _no_setup,
    ) -> None:
        self.db = db
        self.writer = writer
        self.driver = driver
        self.hub = hub
        self.options_for = options_for
        self.targets_for = targets_for or (lambda _project_id: {})
        self.env_for = env_for or (lambda _chat: {})
        self.on_link = on_link
        self.links: dict[str, SandboxClient] = {}
        self.link_info: dict[str, dict[str, Any]] = {}
        self.boots: dict[str, str] = {}
        self.lives: dict[str, LiveChat] = {}
        self.last_active: dict[str, float] = {}  # project id -> last time it was used
        self.closing = False  # the server is shutting down
        self._project_locks: defaultdict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
        self._chat_locks: defaultdict[str, asyncio.Lock] = defaultdict(asyncio.Lock)

    # sandboxes ------------------------------------------------------------------------------

    async def link(self, project_id: str) -> SandboxClient:
        """The project's sandbox connection, (re)connecting when needed."""
        self.touch(project_id)
        async with self._project_locks[project_id]:
            client = self.links.get(project_id)
            if client is not None and not client.closed:
                return client
            link = await self.driver.connect(project_id)
            client = SandboxClient(link, targets=self.targets_for(project_id))
            try:
                hello = await client.start()
                self.link_info[project_id] = await self.on_link(project_id, client)
            except Exception:
                await client.close()
                raise
            self.links[project_id] = client
            self.boots[project_id] = str(hello.info.get("boot", ""))
            return client

    def touch(self, project_id: str) -> None:
        """Someone used the project's sandbox (it is not idle)."""
        self.last_active[project_id] = time.monotonic()

    async def call(self, project_id: str, method: str, params: dict[str, Any] | None = None) -> Any:
        """Call a daemon method of the project's sandbox."""
        client = await self.link(project_id)
        return await client.call(method, params)

    async def forget_project(self, project_id: str) -> None:
        """Close the project's connection and stop following its chats."""
        for live in [lv for lv in self.lives.values() if lv.project_id == project_id]:
            await self._stop_relay(live)
            self.lives.pop(live.chat_id, None)
        client = self.links.pop(project_id, None)
        if client is not None:
            await client.close()

    # chats ----------------------------------------------------------------------------------

    async def live(self, chat: Chat) -> LiveChat:
        """The chat's live state, loaded from the database the first time."""
        live = self.lives.get(chat.id)
        if live is not None:
            return live
        async with self.db.session() as session:
            last = await session.scalar(
                select(func.max(ChatEvent.seq)).where(ChatEvent.chat_id == chat.id)
            )
            last_dseq = await session.scalar(
                select(func.max(ChatEvent.dseq)).where(ChatEvent.chat_id == chat.id)
            )
        live = LiveChat(
            chat_id=chat.id,
            project_id=chat.project_id,
            user_id=chat.user_id,
            next_seq=(last or 0) + 1,
            dseq=last_dseq or 0,
            boot=chat.daemon_boot,
            state=chat.state,
            title=chat.title,
        )
        self.lives[chat.id] = live
        return live

    async def open(self, chat: Chat) -> LiveChat:
        """Make sure the chat runs in its sandbox with its current options and is followed."""
        async with self._chat_locks[chat.id]:
            live = await self.live(chat)
            client = await self.link(chat.project_id)
            boot = self.boots.get(chat.project_id, "")
            if live.boot != boot:  # the daemon restarted: its numbers start again at 1
                live.boot, live.dseq = boot, 0
                await self._save(live.chat_id, daemon_boot=boot)
            options = self.options_for(chat, self.link_info.get(chat.project_id, {}))
            env = self.env_for(chat)
            wanted = json.dumps([options, env], sort_keys=True)
            following = live.relay is not None and not live.relay.done()
            if not following or wanted != live.opened_with:
                params = {"chat_id": chat.id, "options": options, "env": env}
                await client.call("chat.open", params)
                live.opened_with = wanted
            if not following:
                live.relay = asyncio.create_task(self._relay(live, client))
            return live

    async def send(self, chat: Chat, text: str) -> None:
        """Send a message to the chat (starts the chat in its sandbox if needed)."""
        await self.open(chat)
        await self.call(chat.project_id, "chat.send", {"chat_id": chat.id, "text": text})

    async def answer(self, chat: Chat, request_id: str, answer: dict[str, Any]) -> bool:
        """Answer an open request; False if it was already answered."""
        await self.open(chat)
        params = {"chat_id": chat.id, "request_id": request_id, "answer": answer}
        result = await self.call(chat.project_id, "chat.answer", params)
        return bool(isinstance(result, dict) and result.get("accepted"))

    async def cancel(self, chat: Chat) -> None:
        """Stop the chat's running turn."""
        if chat.id in self.lives:
            await self.call(chat.project_id, "chat.cancel", {"chat_id": chat.id})

    async def forget_chat(self, chat: Chat) -> None:
        """Stop the chat's worker and stop following it (before the chat is deleted)."""
        live = self.lives.pop(chat.id, None)
        if live is not None:
            await self._stop_relay(live)
            client = self.links.get(chat.project_id)
            if client is not None and not client.closed:
                with contextlib.suppress(RpcError, ChannelClosed, TimeoutError):
                    await client.call("chat.close", {"chat_id": chat.id}, timeout=20)

    def snapshot(self, chat_id: str) -> dict[str, Any] | None:
        """Live-only state of a followed chat."""
        live = self.lives.get(chat_id)
        return live.snapshot() if live is not None else None

    # following ------------------------------------------------------------------------------

    async def _relay(self, live: LiveChat, client: SandboxClient) -> None:
        try:
            channel = await client.open("chat", {"chat_id": live.chat_id, "after_seq": live.dseq})
            while True:
                message = await channel.recv_message()
                if message.get("type") == "hello":
                    self._sync_pending(live, message.get("pending"))
                elif message.get("type") == "item":
                    seq = message.get("seq")
                    if isinstance(seq, int) and seq > live.dseq:
                        await self._handle(live, seq, message.get("item"))
        except (ChannelClosed, OpenFailed, ProtocolError, ConnectionError):
            pass
        except Exception:
            log.exception("relay of chat %s failed", live.chat_id)
        finally:
            live.relay = None

    def _sync_pending(self, live: LiveChat, pending: Any) -> None:
        if not isinstance(pending, list):
            return
        checked = [check_item(item) for item in pending[:50]]
        live.pending = {item["id"]: item for item in checked if item and item["type"] == "request"}

    async def _handle(self, live: LiveChat, dseq: int, item: Any) -> None:
        checked = check_item(item)
        live.dseq = dseq
        self.last_active[live.project_id] = time.monotonic()
        if checked is None:
            log.warning("dropped an invalid item from chat %s", live.chat_id)
            return
        if is_live_only(checked):
            self._stream(live, checked)
            self.hub.to_chat(
                live.chat_id, {"type": "live", "chat_id": live.chat_id, "item": checked}
            )
            return
        seq, live.next_seq = live.next_seq, live.next_seq + 1
        data = json.dumps(checked, ensure_ascii=False)
        self.writer.add(
            EventRow(
                live.chat_id, seq, dseq, time.time(), checked["type"], event_kind(checked), data
            )
        )
        self.hub.to_chat(
            live.chat_id, {"type": "item", "chat_id": live.chat_id, "seq": seq, "item": checked}
        )
        await self._track(live, checked)

    def _stream(self, live: LiveChat, item: dict[str, Any]) -> None:
        event = item["event"]
        if event["kind"] == "model_delta":
            agent = str(event.get("agent_id", "main"))
            live.streaming[agent] = (live.streaming.get(agent, "") + event["text"])[-MAX_STREAMING:]
        else:
            lines = live.outputs.setdefault(str(event["call_id"]), [])
            lines.append(event["text"])
            del lines[:-MAX_OUTPUT_LINES]

    async def _track(self, live: LiveChat, item: dict[str, Any]) -> None:
        kind = item["type"]
        if kind == "request":
            live.pending[item["id"]] = item
        elif kind == "request_resolved":
            live.pending.pop(item["id"], None)
        elif kind == "event" and item["event"]["kind"] == "model_done":
            live.streaming.pop(str(item["event"].get("agent_id", "main")), None)
        elif kind == "event" and item["event"]["kind"] == "tool_finished":
            live.outputs.pop(str(item["event"]["result"].get("call_id", "")), None)
        elif kind in ("turn", "worker_exited"):
            live.streaming.clear()
            live.outputs.clear()
        changes: dict[str, Any] = {}
        state = next_state(live, item)
        if state != live.state:
            live.state = changes["state"] = state
        if kind == "user" and live.title == "New chat" and item["text"].strip():
            live.title = changes["title"] = item["text"].strip().splitlines()[0][:60]
        if changes:
            await self._save(live.chat_id, **changes, updated_at=time.time())
            self.hub.to_users(
                {live.user_id},
                {
                    "type": "chat_state",
                    "chat_id": live.chat_id,
                    "state": live.state,
                    "title": live.title,
                },
            )

    async def _save(self, chat_id: str, **values: Any) -> None:
        async with self.db.session() as session, session.begin():
            await session.execute(update(Chat).where(Chat.id == chat_id).values(**values))

    async def _stop_relay(self, live: LiveChat) -> None:
        if live.relay is not None:
            live.relay.cancel()
            await asyncio.gather(live.relay, return_exceptions=True)
            live.relay = None

    def working(self, chat_id: str) -> bool:
        """The chat is running a turn or waiting for an answer."""
        live = self.lives.get(chat_id)
        return live is not None and live.state in ("running", "waiting")

    def busy(self, project_id: str) -> bool:
        """A chat of the project is running or waiting for an answer."""
        return any(
            live.project_id == project_id and live.state in ("running", "waiting")
            for live in self.lives.values()
        )

    async def reap(self, idle_seconds: float) -> list[str]:
        """Stop the sandboxes of projects unused for `idle_seconds`; returns their ids."""
        now = time.monotonic()
        stopped = []
        for project_id in list(self.links):
            if self.busy(project_id) or now - self.last_active.get(project_id, now) < idle_seconds:
                continue
            await self.forget_project(project_id)
            await self.driver.stop(project_id)
            stopped.append(project_id)
        return stopped

    async def close(self) -> None:
        """Stop following every chat and close every sandbox connection."""
        self.closing = True
        for live in list(self.lives.values()):
            await self._stop_relay(live)
        for client in list(self.links.values()):
            await client.close()
        self.links.clear()
