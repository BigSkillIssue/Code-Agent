"""Who may read a project's code (W57), and what the others see of its chat.

A project follows its owner's plan (`Limits.code_access`); admins always may. Without code
access, a person gets no files, no git, no terminal and no export, and the chat they watch is
redacted before it leaves the server: tool calls keep their name and the file they touch but not
what they write or read, command output and streamed text are not sent, and code blocks in the
agents' answers are replaced. What stays is what the product does and how many lines changed.
The agents are told the same (the worker's `code_private`), but the server does not rely on it.
"""

import re
from typing import TYPE_CHECKING, Any

from fastapi import HTTPException

from forge_web.db.models import User

if TYPE_CHECKING:  # the hub imports this module, and the services import the hub
    from forge_web.services import Services

HIDDEN = "(hidden: your plan does not include the code)"
CODE_BLOCK = re.compile(r"```[^\n]*\n.*?(?:```|\Z)", re.DOTALL)
INDENTED = re.compile(r"(?:^(?: {4}|\t).*\n?){4,}", re.MULTILINE)  # 4+ indented lines
# Arguments that name what a tool works on, never what it writes: they stay.
KEPT_ARGUMENTS = ("path", "paths", "pattern", "url", "query", "name", "platform", "environment")


async def may_read_code(services: "Services", user: User, project_id: str) -> bool:
    """Whether this person may read the project's code."""
    if user.role == "admin":
        return True
    return (await services.entitlements.of_project(project_id)).code_access


async def require_code(services: "Services", user: User, project_id: str) -> None:
    """403 unless the person may read the project's code."""
    if not await may_read_code(services, user, project_id):
        raise HTTPException(403, "your plan does not include the project's code")


def prose(text: str) -> str:
    """Text without its code: fenced and indented blocks replaced."""
    text = CODE_BLOCK.sub("[code hidden]", text)
    return INDENTED.sub("[code hidden]\n", text)


def redacted_call(call: dict[str, Any]) -> dict[str, Any]:
    """A tool call with only the arguments that name its target."""
    arguments = call.get("arguments")
    kept = {k: v for k, v in arguments.items() if k in KEPT_ARGUMENTS} if isinstance(
        arguments, dict) else {}  # fmt: skip
    return {**call, "arguments": kept}


def redacted_event(event: dict[str, Any]) -> dict[str, Any] | None:
    """An event as someone without code access sees it (None: not at all)."""
    kind = event.get("kind")
    if kind in ("model_delta", "tool_output"):
        return None
    if kind == "tool_started" and isinstance(event.get("call"), dict):
        return {**event, "call": redacted_call(event["call"])}
    if kind == "tool_finished" and isinstance(event.get("result"), dict):
        result = event["result"]
        return {**event, "result": {**result, "text": HIDDEN, "images": []}}
    if kind == "model_done" and isinstance(event.get("message"), dict):
        message = event["message"]
        parts = [
            {**part, "text": prose(part["text"])} if isinstance(part.get("text"), str) else part
            for part in message.get("parts", []) if isinstance(part, dict)
            and part.get("type") != "image"
        ]  # fmt: skip
        calls = [redacted_call(c) for c in message.get("tool_calls", []) if isinstance(c, dict)]
        return {**event, "message": {**message, "parts": parts, "tool_calls": calls}}
    for field in ("text", "report", "summary"):
        if isinstance(event.get(field), str):
            event = {**event, field: prose(event[field])}
    return event


def redacted_item(item: dict[str, Any]) -> dict[str, Any] | None:
    """A stored chat item as someone without code access sees it (None: not at all)."""
    kind = item.get("type")
    if kind == "event" and isinstance(item.get("event"), dict):
        event = redacted_event(item["event"])
        return None if event is None else {**item, "event": event}
    if kind == "request" and item.get("kind") == "approval":
        payload = item.get("payload") or {}
        call = payload.get("call") if isinstance(payload, dict) else None
        if isinstance(call, dict):
            reason = prose(str(payload.get("reason", "")))
            return {**item, "payload": {**payload, "call": redacted_call(call), "reason": reason}}
    if kind == "turn":
        return {**item, "summary": prose(str(item.get("summary", ""))),
                "report": prose(str(item.get("report", "")))}  # fmt: skip
    if kind == "command_result":
        return {**item, "text": prose(str(item.get("text", "")))}
    return item


def redacted_message(message: dict[str, Any]) -> dict[str, Any] | None:
    """A message to a browser without code access (an item, live state or a snapshot)."""
    if message.get("type") == "item" and isinstance(message.get("item"), dict):
        item = redacted_item(message["item"])
        return None if item is None else {**message, "item": item}
    if message.get("type") == "live":
        return None  # streamed text and command output: the stored items follow
    if message.get("type") == "subscribed" and isinstance(message.get("live"), dict):
        live = message["live"]
        pending = [redacted_item(p) or p for p in live.get("pending", []) if isinstance(p, dict)]
        return {**message, "live": {**live, "streaming": {}, "outputs": {}, "pending": pending}}
    return message
