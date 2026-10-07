"""JSON control messages: the handshake, channel opening and request/response calls.

Messages travel in MESSAGE frames. Each one has a `type` that says which model it is.
"""

import json
from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field, TypeAdapter, ValidationError

from forge_sandbox.frames import ProtocolError

PROTOCOL_VERSION = 1


class ErrorInfo(BaseModel):
    """Why something failed: a short machine code and a sentence for people."""

    code: str
    message: str


class Hello(BaseModel):
    """The first message each side sends on the control channel."""

    type: Literal["hello"] = "hello"
    protocol: int = PROTOCOL_VERSION
    role: Literal["server", "daemon"]
    version: str  # package version of the sender
    info: dict[str, Any] = {}  # e.g. the daemon's workspace path and platform


class Request(BaseModel):
    """A call the other side answers with exactly one Response with the same id."""

    type: Literal["request"] = "request"
    id: int
    method: str
    params: dict[str, Any] = {}


class Response(BaseModel):
    """The answer to one Request."""

    type: Literal["response"] = "response"
    id: int
    ok: bool
    result: Any = None
    error: ErrorInfo | None = None


class Notify(BaseModel):
    """A one-way message (no answer)."""

    type: Literal["notify"] = "notify"
    method: str
    params: dict[str, Any] = {}


class OpenRequest(BaseModel):
    """The payload of an OPEN frame: what kind of channel to open, with which arguments."""

    kind: str
    args: dict[str, Any] = {}


ControlMessage = Annotated[Hello | Request | Response | Notify, Field(discriminator="type")]
_CONTROL: TypeAdapter[Hello | Request | Response | Notify] = TypeAdapter(ControlMessage)


def to_json(message: BaseModel | dict[str, Any]) -> bytes:
    """A model or dict as compact UTF-8 JSON."""
    if isinstance(message, BaseModel):
        return message.model_dump_json().encode("utf-8")
    return json.dumps(message, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def from_json(data: bytes) -> dict[str, Any]:
    """Parse one JSON object; ProtocolError if it is not one."""
    try:
        value = json.loads(data)
    except (json.JSONDecodeError, UnicodeDecodeError) as err:
        raise ProtocolError(f"invalid JSON message: {err}") from None
    if not isinstance(value, dict):
        raise ProtocolError("a message must be a JSON object")
    return value


def parse_control(message: dict[str, Any]) -> Hello | Request | Response | Notify:
    """Validate a control-channel message; ProtocolError if it is not one."""
    try:
        return _CONTROL.validate_python(message)
    except ValidationError as err:
        raise ProtocolError(f"invalid control message: {err.errors()[0]['msg']}") from None


def parse_open(payload: bytes) -> OpenRequest:
    """The OpenRequest in an OPEN frame's payload."""
    try:
        return OpenRequest.model_validate(from_json(payload))
    except ValidationError as err:
        raise ProtocolError(f"invalid OPEN request: {err.errors()[0]['msg']}") from None


def parse_error(payload: bytes) -> ErrorInfo:
    """The ErrorInfo in an OPEN_FAIL frame (a generic one if it is malformed)."""
    try:
        return ErrorInfo.model_validate(from_json(payload))
    except (ValidationError, ProtocolError):
        return ErrorInfo(code="open_failed", message="the channel could not be opened")
