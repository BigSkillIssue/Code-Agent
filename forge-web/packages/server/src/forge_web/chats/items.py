"""Chat items from a sandbox: checked, normalized and sorted into stored and live-only ones.

A sandbox is untrusted, so every item is validated against the models it claims to be (Forge's
event models, ToolCall, Question, Usage) and rebuilt from them; anything else is dropped.
"""

import json
from typing import Any

from forge.events import parse_event
from forge.plan import Question
from forge.providers.base import ToolCall, Usage
from pydantic import BaseModel, Field, ValidationError

# Model text and live command output are only shown while they stream: the finished message
# (model_done) and the tool result (tool_finished) carry the same content and are stored.
LIVE_ONLY_EVENTS = frozenset({"model_delta", "tool_output"})
MAX_TEXT = 100_000


class ApprovalPayload(BaseModel):
    """What an approval request shows."""

    call: ToolCall
    reason: str = Field(max_length=MAX_TEXT)


class QuestionPayload(BaseModel):
    """What a question request asks."""

    questions: list[Question] = Field(max_length=10)


class TurnFields(BaseModel):
    """The parts of a finished turn the server keeps."""

    prompt: str = Field(default="", max_length=MAX_TEXT)
    ok: bool = False
    summary: str = Field(default="", max_length=MAX_TEXT)
    report: str = Field(default="", max_length=MAX_TEXT)
    files_changed: list[str] = Field(default_factory=list, max_length=2000)
    assumptions: list[str] = Field(default_factory=list, max_length=200)
    manual_checks: list[str] = Field(default_factory=list, max_length=200)
    usage: Usage = Field(default_factory=Usage)
    seconds: float = 0
    cancelled: bool = False
    error: str = Field(default="", max_length=MAX_TEXT)


def short(value: Any, limit: int = MAX_TEXT) -> str:
    """A string field, cut to `limit`."""
    return str(value)[:limit] if isinstance(value, str | int | float) else ""


def check_item(item: Any) -> dict[str, Any] | None:
    """The item rebuilt from validated fields, or None if it is not a valid chat item."""
    if not isinstance(item, dict):
        return None
    kind = item.get("type")
    try:
        return _CHECKS[kind](item) if isinstance(kind, str) and kind in _CHECKS else None
    except (ValidationError, ValueError, TypeError, KeyError):
        return None


def _event(item: dict[str, Any]) -> dict[str, Any]:
    event = parse_event(json.dumps(item["event"]))
    return {"type": "event", "event": event.model_dump(mode="json")}


def _request(item: dict[str, Any]) -> dict[str, Any] | None:
    request_id, kind = item.get("id"), item.get("kind")
    if not isinstance(request_id, str) or not 0 < len(request_id) <= 64:
        return None
    payload: BaseModel
    if kind == "approval":
        payload = ApprovalPayload.model_validate(item.get("payload"))
    elif kind == "question":
        payload = QuestionPayload.model_validate(item.get("payload"))
    else:
        return None
    return {
        "type": "request",
        "id": request_id,
        "kind": kind,
        "payload": payload.model_dump(mode="json"),
    }


def _resolved(item: dict[str, Any]) -> dict[str, Any] | None:
    request_id = item.get("id")
    if not isinstance(request_id, str):
        return None
    if item.get("cancelled"):
        return {"type": "request_resolved", "id": request_id[:64], "cancelled": True}
    answer = item.get("answer")
    return {
        "type": "request_resolved",
        "id": request_id[:64],
        "answer": answer if isinstance(answer, dict) else {},
    }


def _turn(item: dict[str, Any]) -> dict[str, Any]:
    fields = TurnFields.model_validate(
        {k: v for k, v in item.items() if k in TurnFields.model_fields}
    )
    return {"type": "turn", **fields.model_dump(mode="json")}


def _status(item: dict[str, Any]) -> dict[str, Any] | None:
    state = item.get("state")
    return {"type": "status", "state": state} if state in ("running", "idle") else None


_CHECKS: dict[str, Any] = {
    "event": _event,
    "request": _request,
    "request_resolved": _resolved,
    "turn": _turn,
    "status": _status,
    "user": lambda i: {"type": "user", "text": short(i.get("text"))},
    "ready": lambda i: {"type": "ready", "session_id": short(i.get("session_id"), 64)},
    "command_result": lambda i: {
        "type": "command_result",
        "command": short(i.get("command"), 1000),
        "text": short(i.get("text")),
    },
    "error": lambda i: {"type": "error", "message": short(i.get("message"), 4000)},
    "worker_exited": lambda i: {
        "type": "worker_exited",
        "exit_code": i.get("exit_code") if isinstance(i.get("exit_code"), int) else None,
    },
    "oversized": lambda i: {
        "type": "oversized",
        "original_type": short(i.get("original_type"), 32),
        "event_kind": short(i.get("event_kind"), 32),
    },
}


def is_live_only(item: dict[str, Any]) -> bool:
    """Items shown while they stream but not stored."""
    return item["type"] == "event" and item["event"].get("kind") in LIVE_ONLY_EVENTS


def event_kind(item: dict[str, Any]) -> str:
    """The Forge event kind of an `event` item ('' for other items)."""
    return str(item["event"].get("kind", "")) if item["type"] == "event" else ""
