"""Chats in the daemon: one worker process per chat, a numbered buffer of everything it said,
and `chat` channels that replay the buffer from a given number and then follow it live.

The buffer and the open requests live in the daemon, so a server that reconnects (after a
restart, say) asks for everything after the last number it stored and misses nothing.
"""

import asyncio
import contextlib
import json
import sys
from collections import deque
from collections.abc import Mapping
from typing import Any

from forge.local.local_executor import stop_tree

from forge_sandbox.fsops import Workspace
from forge_sandbox.methods import (
    ChatAnswerParams,
    ChatAttachArgs,
    ChatOpenParams,
    ChatOptions,
    ChatParams,
    ChatSendParams,
    EmptyParams,
    method,
    parse_params,
)
from forge_sandbox.mux import Channel, ChannelClosed, OpenRefused
from forge_sandbox.procs import spawn_options
from forge_sandbox.rpc import Handler, RpcError

WORKER_TYPES = frozenset({"ready", "event", "request", "status", "command_result", "turn", "error"})
BUFFER_ITEMS = 20_000
BACKLOG = 5_000  # items a channel may fall behind before it is dropped (it can re-attach)
MAX_RUNNING = 16
MAX_LINE = 4 * 1024 * 1024
ITEM_LIMIT = 900_000  # bytes of one buffered item (a channel message carries at most 1 MiB)
LONG_STRING = 100_000


def shrink(value: Any) -> Any:
    """The value with very long strings (screenshots, huge outputs) replaced by a note."""
    if isinstance(value, str) and len(value) > LONG_STRING:
        return f"[omitted: {len(value)} characters]"
    if isinstance(value, dict):
        return {k: shrink(v) for k, v in value.items()}
    if isinstance(value, list):
        return [shrink(v) for v in value]
    return value


def fit(item: dict[str, Any]) -> dict[str, Any]:
    """An item small enough for one channel message."""
    if len(json.dumps(item)) <= ITEM_LIMIT:
        return item
    smaller = shrink(item)
    if len(json.dumps(smaller)) <= ITEM_LIMIT:
        return dict(smaller)
    kind = item.get("event", {}).get("kind") if isinstance(item.get("event"), dict) else None
    return {"type": "oversized", "original_type": item.get("type"), "event_kind": kind}


class Chat:
    """One chat: its worker, its buffer, its open requests and who follows it."""

    def __init__(self, chat_id: str) -> None:
        self.id = chat_id
        self.options = ChatOptions()
        self.env: dict[str, str] = {}
        self.process: asyncio.subprocess.Process | None = None
        self.state = "stopped"  # stopped | starting | idle | running
        self.session_id: str | None = None
        self.seq = 0
        self.buffer: deque[tuple[int, dict[str, Any]]] = deque(maxlen=BUFFER_ITEMS)
        self.pending: dict[str, dict[str, Any]] = {}
        self.listeners: set[asyncio.Queue[tuple[int, dict[str, Any]] | None]] = set()
        self.lock = asyncio.Lock()

    @property
    def alive(self) -> bool:
        """The worker process is running."""
        return self.process is not None and self.process.returncode is None

    def oldest(self) -> int:
        """The number of the oldest item still buffered (seq + 1 when empty)."""
        return self.buffer[0][0] if self.buffer else self.seq + 1

    def info(self) -> dict[str, Any]:
        """What chat.* calls and a channel's hello report."""
        return {
            "chat_id": self.id,
            "state": self.state,
            "seq": self.seq,
            "session_id": self.session_id,
            "pending": list(self.pending.values()),
        }

    def append(self, item: dict[str, Any]) -> int:
        """Number an item, buffer it and hand it to every follower."""
        self.seq += 1
        entry = (self.seq, fit(item))
        self.buffer.append(entry)
        for queue in list(self.listeners):
            if queue.qsize() > BACKLOG:
                self.listeners.discard(queue)
                queue.put_nowait(None)  # too far behind: drop it; it re-attaches
            else:
                queue.put_nowait(entry)
        return self.seq

    async def write(self, message: dict[str, Any]) -> None:
        """Send a message to the worker."""
        if not self.alive or self.process is None or self.process.stdin is None:
            raise RpcError("not_running", f"chat {self.id} has no running worker")
        async with self.lock:
            self.process.stdin.write(json.dumps(message).encode("utf-8") + b"\n")
            await self.process.stdin.drain()


class Chats:
    """Starts chat workers and serves their buffers."""

    def __init__(self, workspace: Workspace, env: Mapping[str, str], python: str = sys.executable):
        self.workspace = workspace
        self.env = dict(env)
        self.python = python
        self.chats: dict[str, Chat] = {}
        self.spare: asyncio.subprocess.Process | None = None  # a worker waiting for a chat
        self._warming = asyncio.Lock()  # one spare at a time
        self._readers: set[asyncio.Task[None]] = set()

    def handlers(self) -> dict[str, Handler]:
        """chat.* methods."""
        return {
            "chat.open": method(ChatOpenParams, self.open),
            "chat.send": method(ChatSendParams, self.send),
            "chat.answer": method(ChatAnswerParams, self.answer),
            "chat.cancel": method(ChatParams, self.cancel),
            "chat.close": method(ChatParams, self.close_one),
            "chat.list": method(EmptyParams, self.list),
            "chat.warm": method(EmptyParams, self.warm),
        }

    async def open(self, params: ChatOpenParams) -> dict[str, Any]:
        """Create the chat if needed and make sure its worker runs with these options."""
        chat = self.chats.setdefault(params.chat_id, Chat(params.chat_id))
        changed = (chat.options, chat.env) != (params.options, params.env)
        chat.options, chat.env = params.options, params.env
        if chat.alive and changed and chat.state != "running":
            await self._stop(chat)  # new options take effect with a fresh worker
        if not chat.alive:
            await self._spawn(chat)
        return chat.info()

    async def warm(self, _params: EmptyParams) -> dict[str, bool]:
        """Start a spare worker (Forge imported, no chat yet) unless one is waiting."""
        async with self._warming:
            if self.spare is None or self.spare.returncode is not None:
                self.spare = await self._start_worker()
        return {"warm": True}

    async def _start_worker(self) -> asyncio.subprocess.Process:
        # -I: never import modules from the workspace (the agent writes there).
        argv = [self.python, "-I", "-m", "forge_sandbox", "worker",
                "--workspace", str(self.workspace.root)]  # fmt: skip
        try:
            return await asyncio.create_subprocess_exec(
                argv[0],
                *argv[1:],
                cwd=self.workspace.root,
                env=self.env,  # the chat's own variables come with `start`
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                limit=MAX_LINE,
                **spawn_options(self.workspace.owner),
            )
        except OSError as err:
            raise RpcError("start_failed", f"could not start the chat worker: {err}") from None

    async def _spawn(self, chat: Chat) -> None:
        if sum(c.alive for c in self.chats.values()) >= MAX_RUNNING:
            raise RpcError("too_many", f"at most {MAX_RUNNING} chats may run at once")
        process, self.spare = self.spare, None
        if process is None or process.returncode is not None:
            process = await self._start_worker()
        chat.process, chat.state = process, "starting"
        await chat.write({"type": "start", "chat_id": chat.id, "env": chat.env,
                          "options": chat.options.model_dump(mode="json")})  # fmt: skip
        task = asyncio.create_task(self._read(chat, process))
        self._readers.add(task)
        task.add_done_callback(self._readers.discard)
        self._background(self.warm(EmptyParams()))  # the next chat starts at once

    def _background(self, work: Any) -> None:
        """Run work on the side; its failure only means no spare (the next chat starts cold)."""
        task = asyncio.ensure_future(work)
        self._readers.add(task)
        task.add_done_callback(self._readers.discard)
        task.add_done_callback(lambda done: done.cancelled() or done.exception())  # retrieved

    async def _read(self, chat: Chat, process: asyncio.subprocess.Process) -> None:
        assert process.stdout is not None
        while True:
            try:
                line = await process.stdout.readline()
            except ValueError:  # a line over MAX_LINE: the worker is misbehaving
                break
            if not line:
                break
            with contextlib.suppress(ValueError):
                self._received(chat, json.loads(line))
        code = await process.wait()
        if chat.process is process:
            chat.process, chat.state = None, "stopped"
            for request_id in list(chat.pending):
                chat.pending.pop(request_id)
                chat.append({"type": "request_resolved", "id": request_id, "cancelled": True})
            chat.append({"type": "worker_exited", "exit_code": code})

    def _received(self, chat: Chat, item: Any) -> None:
        if not isinstance(item, dict) or item.get("type") not in WORKER_TYPES:
            return
        kind = item["type"]
        if kind == "ready":
            chat.state = "idle"
            chat.session_id = str(item.get("session_id") or "") or None
        elif kind == "status" and item.get("state") in ("idle", "running"):
            chat.state = item["state"]
        elif kind == "request":
            if not isinstance(item.get("id"), str) or item.get("kind") not in (
                "approval",
                "question",
            ):
                return
            chat.pending[item["id"]] = item
        chat.append(item)

    async def send(self, params: ChatSendParams) -> dict[str, Any]:
        """Send a prompt (or slash command); restarts a stopped worker first."""
        chat = self._get(params.chat_id)
        if not chat.alive:
            await self._spawn(chat)
        if chat.state == "running":
            raise RpcError("busy", "the chat is still working on the last message")
        chat.state = "running"
        chat.append({"type": "user", "text": params.text})
        await chat.write({"type": "prompt", "text": params.text})
        return chat.info()

    async def answer(self, params: ChatAnswerParams) -> dict[str, Any]:
        """Answer an open request; only the first answer counts."""
        chat = self._get(params.chat_id)
        if chat.pending.pop(params.request_id, None) is None:
            return {"accepted": False}
        await chat.write({"type": "answer", "id": params.request_id, "answer": params.answer})
        chat.append({"type": "request_resolved", "id": params.request_id, "answer": params.answer})
        return {"accepted": True}

    async def cancel(self, params: ChatParams) -> dict[str, Any]:
        """Stop the running turn."""
        chat = self._get(params.chat_id)
        if chat.alive:
            await chat.write({"type": "cancel"})
        return chat.info()

    async def close_one(self, params: ChatParams) -> dict[str, Any]:
        """Stop the chat's worker (the chat and its buffer stay)."""
        chat = self._get(params.chat_id)
        await self._stop(chat)
        return chat.info()

    async def _stop(self, chat: Chat) -> None:
        process = chat.process
        if process is None or process.returncode is not None:
            return
        with contextlib.suppress(RpcError, ConnectionError):
            await chat.write({"type": "shutdown"})
        try:
            await asyncio.wait_for(process.wait(), 10)
        except TimeoutError:
            await stop_tree(process)
        await asyncio.sleep(0)  # let the reader record the exit

    async def list(self, _params: EmptyParams) -> list[dict[str, Any]]:
        """Every chat this daemon knows."""
        return [chat.info() for chat in self.chats.values()]

    def _get(self, chat_id: str) -> Chat:
        chat = self.chats.get(chat_id)
        if chat is None:
            raise RpcError("not_found", f"no chat {chat_id}; open it first")
        return chat

    async def attach(self, channel: Channel) -> None:
        """Serve a `chat` channel: hello, missed items after `after_seq`, then live items."""
        args = parse_params(ChatAttachArgs, channel.args)
        chat = self.chats.get(args.chat_id)
        if chat is None:
            raise OpenRefused("not_found", f"no chat {args.chat_id}")
        queue: asyncio.Queue[tuple[int, dict[str, Any]] | None] = asyncio.Queue()
        backlog = [entry for entry in chat.buffer if entry[0] > args.after_seq]
        chat.listeners.add(queue)  # no await since the snapshot: nothing slips between
        watcher = asyncio.create_task(_until_closed(channel))
        try:
            await channel.accept()
            await channel.send_message({"type": "hello", **chat.info(), "oldest": chat.oldest()})
            if args.after_seq + 1 < chat.oldest():
                await channel.send_message({"type": "gap", "from": args.after_seq + 1})
            for seq, item in backlog:
                await channel.send_message({"type": "item", "seq": seq, "item": item})
            await _follow(channel, queue, watcher)
        finally:
            chat.listeners.discard(queue)
            watcher.cancel()

    async def close(self) -> None:
        """Stop every worker (and the spare)."""
        for chat in self.chats.values():
            await self._stop(chat)
        spare, self.spare = self.spare, None
        if spare is not None and spare.returncode is None:
            if spare.stdin is not None:
                spare.stdin.close()  # a spare ends when its input does
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(spare.wait(), 10)
            if spare.returncode is None:
                await stop_tree(spare)
        for task in self._readers:
            task.cancel()


async def _until_closed(channel: Channel) -> None:
    """Return when the server closes the channel (it sends nothing else)."""
    with contextlib.suppress(ChannelClosed):
        while True:
            await channel.receive()


async def _follow(
    channel: Channel,
    queue: asyncio.Queue[tuple[int, dict[str, Any]] | None],
    watcher: asyncio.Task[None],
) -> None:
    while not watcher.done():
        getter = asyncio.ensure_future(queue.get())
        await asyncio.wait({getter, watcher}, return_when=asyncio.FIRST_COMPLETED)
        if not getter.done():
            getter.cancel()
            return
        entry = getter.result()
        if entry is None:
            return
        seq, item = entry
        await channel.send_message({"type": "item", "seq": seq, "item": item})
