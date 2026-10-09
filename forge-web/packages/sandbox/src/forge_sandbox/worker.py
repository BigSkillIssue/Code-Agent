"""One chat's Forge: `forge-sandbox worker`, started by the daemon, one JSON message per line.

From the daemon: start (once, first: the chat, its options and its variables, such as the run
token), prompt, answer, cancel, shutdown. A worker can start before it has a chat (a warm spare
that has already imported Forge) and learns which chat it serves from `start`.
To the daemon: ready, event (every Forge event), request (approval or question), status,
command_result, turn (a finished turn), error.

The worker keeps the original stdin/stdout for the protocol on private descriptors and points
fd 0 at /dev/null and fd 1 at stderr, so nothing else that prints can corrupt the stream. On
Linux it also makes itself non-dumpable, so programs the agent starts (same user) can neither
read its memory nor write into its pipes.
"""

import asyncio
import contextlib
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

from forge.commands import HELP, custom_commands, handle_command
from forge.config import ConfigError, ForgeConfig, forge_home, load_config
from forge.ctx import Ctx
from forge.events import Event
from forge.local.memory_bus import MemoryBus
from forge.local.sqlite_store import SqliteStore
from forge.pipeline import PipelineError, Report, report_text, resume, run_task
from forge.ports import SessionNotFoundError
from forge.providers.base import ProviderError
from forge.providers.fake import FakeProvider
from forge.wiring import close_session, install_fake, open_session
from pydantic import ValidationError

from forge_sandbox.history import ChatState, Turn, with_history
from forge_sandbox.methods import ChatOptions
from forge_sandbox.mux import Writer
from forge_sandbox.pipe_renderer import PipeRenderer

ROLES = ("refiner", "planner", "coder", "reviewer", "compressor", "explore", "tester",
         "researcher", "browser", "lead")  # fmt: skip
# Tools that ask for approval by default; "auto" mode allows them instead of denying them.
AUTO_ALLOW = ["web_fetch", "web_search", "remember", "browser_open"]
POLICY = {"ask": "always", "edits": "on-request", "auto": "never"}
MAX_LINE = 4 * 1024 * 1024


def config_overrides(options: ChatOptions) -> dict[str, Any]:
    """Forge config overrides for a chat. They win over the project's own config file, so a
    repository cannot loosen approvals or the sandbox for itself."""
    overrides: dict[str, Any] = {
        "approval.policy": POLICY[options.mode],
        "permissions.allow": list(AUTO_ALLOW) if options.mode == "auto" else [],
        "sandbox.mode": options.sandbox_mode,
    }
    roles = {role: [options.model] for role in ROLES} if options.model else {}
    roles.update(options.roles)
    if roles:
        overrides["roles"] = roles
    if options.max_cost_usd is not None:
        overrides["limits.max_cost_usd"] = options.max_cost_usd
    for name, provider in options.providers.items():
        overrides[f"providers.{name}"] = provider
    return overrides


def build_config(root: Path, options: ChatOptions) -> ForgeConfig:
    """The effective Forge config of a chat."""
    cfg = load_config(root, overrides=config_overrides(options))
    if options.fake_script is not None:
        install_fake(cfg, FakeProvider.from_data(options.fake_script))
    return cfg


class ForwardingBus(MemoryBus):
    """Forge's event bus, which also sends each event to the daemon as it is published: a
    request (sent straight away) then never overtakes the events that led to it."""

    def __init__(self, out: "Outbox") -> None:
        super().__init__()
        self.out = out

    async def publish(self, event: Event) -> None:
        """Deliver to subscribers, then send to the daemon."""
        await super().publish(event)
        with contextlib.suppress(OSError):  # the daemon is gone; the worker ends on its own
            await self.out.send({"type": "event", "event": event.model_dump(mode="json")})


class ChatWorker:
    """Runs a chat's turns on one Forge session."""

    def __init__(self, root: Path, chat_id: str, options: ChatOptions, out: "Outbox") -> None:
        self.root = root
        self.options = options
        self.out = out
        self.home = forge_home()
        self.state = ChatState.load(self.home, chat_id)
        self.renderer = PipeRenderer(out.send, auto_plans=options.mode == "auto")
        self.bus = ForwardingBus(out)
        self.ctx: Ctx | None = None
        self.task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        """Open (or reopen) the chat's Forge session; its events go to the daemon as they happen."""
        cfg = build_config(self.root, self.options)
        store = SqliteStore(self.home / "forge.db")
        session = None
        if self.state.session_id:
            with contextlib.suppress(SessionNotFoundError):
                session = await store.load_session(self.state.session_id)
        self.ctx = await open_session(
            self.root, cfg, self.renderer, store=store, bus=self.bus, session=session
        )
        self.state.session_id = self.ctx.session.id
        self.state.save(self.home)
        await self.out.send({
            "type": "ready", "session_id": self.ctx.session.id, "turns": len(self.state.turns),
            "commands": slash_commands(self.root),
        })  # fmt: skip

    def submit(self, text: str) -> bool:
        """Start a turn; False while another one runs."""
        if self.task is not None and not self.task.done():
            return False
        self.task = asyncio.create_task(self.turn(text))
        return True

    async def cancel(self) -> None:
        """Stop the running turn."""
        if self.task is not None and not self.task.done():
            self.task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self.task

    async def turn(self, text: str) -> None:
        """Run one prompt or slash command and report how it ended."""
        assert self.ctx is not None
        started = time.time()
        await self.out.send({"type": "status", "state": "running"})
        result: dict[str, Any] = {"type": "turn", "prompt": text, "ok": False}
        try:
            result.update(await self._run(text))
        except asyncio.CancelledError:
            result.update(cancelled=True, summary="Stopped by the user.")
            self.ctx.session.status = "cancelled"
            with contextlib.suppress(Exception):
                await self.ctx.store.save_session(self.ctx.session)
        except (PipelineError, ProviderError, ConfigError) as err:
            result.update(error=str(err), summary=f"The turn failed: {err}")
        result["seconds"] = round(time.time() - started, 2)
        if not text.startswith("/") or result.get("ran_prompt"):
            summary = str(result.get("summary", ""))
            self.state.turns.append(Turn(prompt=text, summary=summary, ok=bool(result["ok"])))
            self.state.save(self.home)
        await self.out.send(result)
        await self.out.send({"type": "status", "state": "idle"})

    async def _run(self, text: str) -> dict[str, Any]:
        assert self.ctx is not None
        if not text.startswith("/"):
            return report_fields(await run_task(with_history(text, self.state.turns), self.ctx))
        command = await handle_command(self.ctx, text)
        if command.text:
            await self.out.send({"type": "command_result", "command": text, "text": command.text})
        if command.prompt is not None:
            report = await run_task(with_history(command.prompt, self.state.turns), self.ctx)
            return {**report_fields(report), "ran_prompt": True}
        if command.resume:
            plan = await resume(self.ctx)
            ok = all(s.status in ("done", "skipped") for s in plan.steps)
            return {"ok": ok, "summary": "All steps done." if ok else "Some steps did not finish."}
        return {"ok": True, "summary": command.text}

    async def close(self) -> None:
        """Stop the turn and close the Forge session."""
        await self.cancel()
        if self.ctx is not None:
            await close_session(self.ctx)


def slash_commands(root: Path) -> list[dict[str, Any]]:
    """Forge's slash commands and the custom ones of the user and the project, for completion."""
    found = [{"name": usage.split()[0], "usage": usage, "help": text, "custom": False}
             for usage, text in HELP.items()]  # fmt: skip
    try:
        custom = custom_commands(root)
    except (OSError, UnicodeDecodeError):  # an unreadable command file must not stop the chat
        custom = {}
    found += [{"name": f"/{c.name}", "usage": f"/{c.name} [arguments]",
               "help": c.description, "custom": True} for c in custom.values()]  # fmt: skip
    return found


def report_fields(report: Report) -> dict[str, Any]:
    """A Forge report as fields of a `turn` message."""
    return {
        "ok": report.ok,
        "summary": report.summary,
        "report": report_text(report),
        "files_changed": report.files_changed,
        "assumptions": report.assumptions,
        "manual_checks": report.manual_checks,
        "usage": report.usage.model_dump(),
    }


class Outbox:
    """Writes protocol messages, one JSON object per line."""

    def __init__(self, writer: Writer) -> None:
        self.writer = writer

    async def send(self, message: dict[str, Any]) -> None:
        """Write one message."""
        self.writer.write(json.dumps(message, separators=(",", ":")).encode("utf-8") + b"\n")
        await self.writer.drain()


def protect_stdio() -> tuple[int, int]:
    """Move the protocol off fds 0 and 1; returns (protocol in, protocol out)."""
    proto_in, proto_out = os.dup(0), os.dup(1)
    devnull = os.open(os.devnull, os.O_RDONLY)
    os.dup2(devnull, 0)
    os.close(devnull)
    os.dup2(2, 1)
    sys.stdout = sys.stderr
    return proto_in, proto_out


def make_undumpable() -> None:
    """Linux: keep other processes of the same user out of this one (memory, fds, environ)."""
    if sys.platform.startswith("linux"):
        import ctypes

        with contextlib.suppress(OSError, AttributeError):
            ctypes.CDLL(None).prctl(4, 0, 0, 0, 0)  # PR_SET_DUMPABLE = 4


async def protocol_streams(fd_in: int, fd_out: int) -> tuple[asyncio.StreamReader, Outbox]:
    """Asyncio streams over the protocol descriptors."""
    loop = asyncio.get_running_loop()
    reader = asyncio.StreamReader(limit=MAX_LINE)
    if sys.platform == "win32":
        from forge_sandbox.streams import ThreadWriter, feed_from_thread

        feed_from_thread(reader, fd_in)
        return reader, Outbox(ThreadWriter(fd_out))
    await loop.connect_read_pipe(
        lambda: asyncio.StreamReaderProtocol(reader), os.fdopen(fd_in, "rb", buffering=0)
    )
    transport, protocol = await loop.connect_write_pipe(
        asyncio.streams.FlowControlMixin, os.fdopen(fd_out, "wb", buffering=0)
    )
    return reader, Outbox(asyncio.StreamWriter(transport, protocol, reader, loop))


async def read_message(reader: asyncio.StreamReader) -> dict[str, Any] | None:
    """The next message from the daemon, or None at the end."""
    line = await reader.readline()
    if not line:
        return None
    value = json.loads(line)
    return value if isinstance(value, dict) else {}


async def serve(root: Path, chat_id: str, reader: asyncio.StreamReader, out: Outbox) -> int:
    """Run the worker until shutdown or the end of input."""
    first = await read_message(reader)
    if first is None:
        return 0  # a spare that was never used
    chat_id = chat_id or str(first.get("chat_id") or "")
    try:
        options = ChatOptions.model_validate(first.get("options", {}))
    except ValidationError as err:
        await out.send({"type": "error", "message": f"invalid chat options: {err}"})
        return 2
    if not chat_id:
        await out.send({"type": "error", "message": "start names no chat"})
        return 2
    use_variables(first.get("env"))
    worker = ChatWorker(root, chat_id, options, out)
    try:
        await worker.start()
    except ConfigError as err:
        await out.send({"type": "error", "message": f"invalid configuration: {err}"})
        return 2
    try:
        while (message := await read_message(reader)) is not None:
            if not await handle(worker, message, out):
                break
    finally:
        await worker.close()
    return 0


def use_variables(env: Any) -> None:
    """The chat's own variables (the run token), before Forge reads its configuration."""
    if isinstance(env, dict):
        for name, value in env.items():
            if isinstance(name, str) and isinstance(value, str) and name and "=" not in name:
                os.environ[name] = value


async def handle(worker: ChatWorker, message: dict[str, Any], out: Outbox) -> bool:
    """Act on one daemon message; False means stop."""
    kind = message.get("type")
    if kind == "prompt" and isinstance(message.get("text"), str):
        if not worker.submit(message["text"]):
            await out.send({"type": "error", "message": "a turn is already running"})
    elif kind == "answer":
        worker.renderer.resolve(str(message.get("id", "")), message.get("answer"))
    elif kind == "cancel":
        await worker.cancel()
    elif kind == "shutdown":
        return False
    return True


async def run_worker(root: Path, chat_id: str = "") -> int:
    """`forge-sandbox worker`: protect stdio, then serve."""
    make_undumpable()
    fd_in, fd_out = protect_stdio()
    reader, out = await protocol_streams(fd_in, fd_out)
    return await serve(root.resolve(), chat_id, reader, out)
