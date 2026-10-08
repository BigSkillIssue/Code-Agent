"""`forge-web dev-chat`: chat with Forge through a local sandbox, in the terminal.

For trying out and debugging the sandbox and worker before the web UI exists; it uses the same
SandboxClient and chat channel the server uses.
"""

import argparse
import asyncio
import contextlib
import json
import os
import sys
from pathlib import Path
from typing import Any

from forge_sandbox.mux import Channel, ChannelClosed
from forge_web.containers.local import LocalDriver
from forge_web.fake import fake_script
from forge_web.sandbox_client import SandboxClient
from forge_web.settings import WebSettings


def add_arguments(parser: argparse.ArgumentParser) -> None:
    """The dev-chat options."""
    parser.add_argument(
        "--fake", nargs="?", const="", metavar="SCRIPT", help="use the fake model (optional script)"
    )
    parser.add_argument("--workspace", type=Path, help="work in this folder (default: data folder)")
    parser.add_argument("--project", default="dev-chat", help="project id (default: dev-chat)")
    parser.add_argument("--chat", default="dev", help="chat id (default: dev)")
    parser.add_argument("--prompt", help="send one message, print the answer and exit")
    parser.add_argument("--mode", choices=["ask", "edits", "auto"], default="edits")
    parser.add_argument("--model", help="provider/model for every role")
    parser.add_argument("--env", action="append", default=[], help="pass this variable through")
    parser.add_argument("--yes", action="store_true", help="approve every request")


def chat_options(args: argparse.Namespace) -> dict[str, Any]:
    """chat.open options from the command line."""
    options: dict[str, Any] = {"mode": args.mode}
    if args.model:
        options["model"] = args.model
    if args.fake is not None:
        options["fake_script"] = fake_script(args.fake)
    return options


async def dev_chat(settings: WebSettings, args: argparse.Namespace) -> int:
    """Open the chat, then send one prompt or read prompts from stdin."""
    folders = {args.project: args.workspace.resolve()} if args.workspace else {}
    driver = LocalDriver(settings.data_dir, folders=folders)
    client = SandboxClient(await driver.connect(args.project))
    await client.start()
    try:
        env = {name: os.environ[name] for name in args.env if name in os.environ}
        await client.call(
            "chat.open", {"chat_id": args.chat, "options": chat_options(args), "env": env}
        )
        channel = await client.open("chat", {"chat_id": args.chat})
        printer = Printer(client, args.chat, approve_all=args.yes)
        follower = asyncio.create_task(printer.follow(channel))
        prompts = [args.prompt] if args.prompt else None
        ok = await converse(client, args.chat, printer, prompts)
        follower.cancel()
        return 0 if ok else 1
    finally:
        with contextlib.suppress(Exception):
            await client.call("chat.close", {"chat_id": args.chat}, timeout=15)
        await client.close()


async def converse(
    client: SandboxClient, chat_id: str, printer: "Printer", prompts: list[str] | None
) -> bool:
    """Send each prompt (or each line typed) and wait for its turn to end."""
    ok = True
    while True:
        if prompts is not None:
            if not prompts:
                return ok
            text = prompts.pop(0)
        else:
            try:
                text = (await asyncio.to_thread(input, "\nyou> ")).strip()
            except EOFError:
                return ok
            if not text:
                continue
        printer.turn_done.clear()
        await client.call("chat.send", {"chat_id": chat_id, "text": text})
        await printer.turn_done.wait()
        ok = printer.last_ok


class Printer:
    """Shows chat items as text and answers requests (asking on stdin unless approve_all)."""

    def __init__(self, client: SandboxClient, chat_id: str, *, approve_all: bool) -> None:
        self.client = client
        self.chat_id = chat_id
        self.approve_all = approve_all
        self.turn_done = asyncio.Event()
        self.last_ok = True

    async def follow(self, channel: Channel) -> None:
        """Print every item until the channel ends."""
        with contextlib.suppress(ChannelClosed):
            while True:
                message = await channel.recv_message()
                if message.get("type") == "item":
                    await self.show(message["item"])

    async def show(self, item: dict[str, Any]) -> None:
        """Print one item."""
        kind = item.get("type")
        if kind == "event":
            self.show_event(item["event"])
        elif kind == "request":
            await self.answer(item)
        elif kind == "command_result":
            print(item.get("text", ""))
        elif kind == "turn":
            print("\n" + str(item.get("report") or item.get("summary") or ""))
            self.last_ok = bool(item.get("ok"))
            self.turn_done.set()
        elif kind in ("error", "worker_exited"):
            print(f"\n[{kind}] {item.get('message', item.get('exit_code', ''))}", file=sys.stderr)
            if kind == "worker_exited":
                self.last_ok = False
                self.turn_done.set()

    def show_event(self, event: dict[str, Any]) -> None:
        """Print a Forge event the way the plain CLI does, roughly."""
        kind = event.get("kind")
        if kind == "model_delta":
            print(event.get("text", ""), end="", flush=True)
        elif kind == "tool_started":
            call = event.get("call", {})
            print(f"\n▶ {call.get('name')} {json.dumps(call.get('arguments', {}))[:120]}")
        elif kind == "tool_output":
            print(f"  {event.get('text', '').rstrip()}")
        elif kind == "tool_finished":
            result = event.get("result", {})
            mark = "✓" if result.get("ok") else "✗"
            print(f"{mark} {str(result.get('text', '')).strip()[:200]}")
        elif kind == "error":
            print(f"\n[error] {event.get('message')}", file=sys.stderr)

    async def answer(self, request: dict[str, Any]) -> None:
        """Answer an approval or a question."""
        payload = request.get("payload", {})
        if request.get("kind") == "approval":
            allow = self.approve_all or await self.confirm(
                f"\nAllow {payload.get('call', {}).get('name')}? {payload.get('reason', '')} [y/N] "
            )
            answer: dict[str, Any] = {"allow": allow}
        else:
            answer = {"answers": await self.ask(payload.get("questions", []))}
        params = {"chat_id": self.chat_id, "request_id": request["id"], "answer": answer}
        await self.client.call("chat.answer", params)

    async def confirm(self, prompt: str) -> bool:
        """y/N on stdin."""
        reply = await asyncio.to_thread(input, prompt)
        return reply.strip().lower() in ("y", "yes", "j", "ja")

    async def ask(self, questions: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """One answer per question: the default, the first option, or typed text."""
        answers = []
        for index, question in enumerate(questions):
            options = question.get("options") or []
            fallback = question.get("default") or (options[0] if options else "")
            if self.approve_all:
                value = fallback
            else:
                hint = f" ({' / '.join(options)})" if options else ""
                typed = await asyncio.to_thread(input, f"\n{question.get('text')}{hint} ")
                value = typed.strip() or fallback
            answers.append({"question_index": index, "values": [value]})
        return answers
