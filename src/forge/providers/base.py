"""The internal message format every provider adapter translates to and from."""

from typing import Any, Literal

from pydantic import BaseModel, Field

Role = Literal["system", "user", "assistant", "tool"]


class TextPart(BaseModel):
    """Plain text inside a message."""

    type: Literal["text"] = "text"
    text: str


class ImagePart(BaseModel):
    """An image inside a message, base64 encoded."""

    type: Literal["image"] = "image"
    media_type: str  # "image/png"
    data_b64: str


class ToolCall(BaseModel):
    """A model's request to run one tool."""

    id: str  # provider's id, or "call_<n>" if none
    name: str
    arguments: dict[str, Any]


class ToolResult(BaseModel):
    """What a tool returned, as the model will see it."""

    call_id: str
    ok: bool
    text: str  # what the model sees (already capped)
    spill_path: str | None = None  # full output file when capped
    code: str | None = None  # error code when ok=False (see docs/TOOLS.md)
    images: list["ImagePart"] = []  # images for vision models (read_file, MCP)


class Message(BaseModel):
    """One turn of a conversation in the provider-neutral format."""

    role: Role
    parts: list[TextPart | ImagePart] = Field(default_factory=list)
    tool_calls: list[ToolCall] = Field(default_factory=list)  # assistant only
    tool_result: ToolResult | None = None  # tool only
    reasoning: str | None = None  # thinking text, kept only if provider needs it back

    def text(self) -> str:
        """All text parts joined by newlines."""
        return "\n".join(part.text for part in self.parts if isinstance(part, TextPart))


class Usage(BaseModel):
    """Token counts and cost of one or more model calls."""

    input_tokens: int = 0
    output_tokens: int = 0
    cached_tokens: int = 0
    cost_usd: float = 0.0

    def __add__(self, other: "Usage") -> "Usage":
        return Usage(
            input_tokens=self.input_tokens + other.input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
            cached_tokens=self.cached_tokens + other.cached_tokens,
            cost_usd=self.cost_usd + other.cost_usd,
        )


def text_message(role: Role, text: str) -> Message:
    """A message holding a single text part."""
    return Message(role=role, parts=[TextPart(text=text)])
