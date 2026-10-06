"""The internal message format every provider adapter translates to and from."""

from collections.abc import AsyncIterator
from typing import Any, Literal, Protocol

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


class ToolSpec(BaseModel):
    """A tool as the model sees it: name, description and JSON Schema of its arguments."""

    name: str
    description: str
    parameters: dict[str, Any]  # JSON Schema (object)


class Capabilities(BaseModel):
    """What a model can do and what it costs."""

    context_window: int = 32_000
    max_output: int = 4_096
    tools: bool = True  # native tool calling
    parallel_tools: bool = False
    vision: bool = False
    reasoning: bool = False
    prompt_cache: bool = False
    web_search: bool = False  # provider-native search tool
    cost_in: float = 0.0  # USD per 1M input tokens
    cost_out: float = 0.0


class ChatRequest(BaseModel):
    """One model call."""

    model: str  # model id as the provider knows it
    system: str
    messages: list[Message]
    tools: list[ToolSpec] = []
    max_output: int | None = None
    temperature: float | None = None
    reasoning_effort: Literal["low", "medium", "high"] | None = None
    json_schema: dict[str, Any] | None = None  # structured output when supported


class StreamItem(BaseModel):
    """What `stream()` yields: text deltas and finished tool calls, then one item with `done`."""

    delta: str = ""  # text chunk
    tool_call: ToolCall | None = None  # a call complete before the reply ends (S52); also in done
    done: Message | None = None  # final assistant message (last item only)
    usage: Usage | None = None  # with the last item


class Provider(Protocol):
    """One API endpoint (vendor, gateway or local server)."""

    name: str  # "openai", "anthropic", "openrouter", ...

    def stream(self, req: ChatRequest) -> AsyncIterator[StreamItem]: ...
    async def count_tokens(self, req: ChatRequest) -> int: ...
    def capabilities(self, model: str) -> Capabilities: ...


ErrorKind = Literal[
    "auth", "rate_limit", "overloaded", "context_overflow", "bad_request", "network"
]
RETRYABLE: frozenset[ErrorKind] = frozenset({"rate_limit", "overloaded", "network"})


class ProviderError(Exception):
    """A provider call failed after its retries, or with an error retrying cannot fix."""

    kind: ErrorKind
    retry_after_s: float | None

    def __init__(self, kind: ErrorKind, message: str = "", retry_after_s: float | None = None):
        super().__init__(message or kind)
        self.kind = kind
        self.retry_after_s = retry_after_s


def cost_usd(usage: Usage, caps: Capabilities) -> float:
    """Price of `usage` at the model's per-million-token rates."""
    return (usage.input_tokens * caps.cost_in + usage.output_tokens * caps.cost_out) / 1_000_000
