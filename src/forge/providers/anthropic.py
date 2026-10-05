"""Adapter for Claude: the Anthropic API, Amazon Bedrock and Google Vertex AI.

Uses the official `anthropic` SDK. Keeps the system prompt cacheable (cache_control on the
stable prefix), maps `reasoning_effort` to adaptive thinking with an effort level, and
round-trips thinking blocks so tool loops can continue on the same model.
"""

import asyncio
import json
import os
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from typing import Any

import anthropic
import httpx2

from forge.config import ModelOverride, ProviderConfig
from forge.providers.base import (
    Capabilities,
    ChatRequest,
    ImagePart,
    Message,
    ProviderError,
    StreamItem,
    TextPart,
    ToolCall,
    Usage,
)
from forge.providers.catalog import capabilities_for
from forge.providers.errors import OVERFLOW_HINTS, error_from_status
from forge.providers.retry import RETRY_DELAYS, Sleep, stream_with_retries
from forge.providers.tokens import estimate_tokens

DEFAULT_MAX_OUTPUT = 32_000
CACHE_READ_PRICE = 0.1  # cache reads cost a tenth of normal input tokens
CACHE_WRITE_PRICE = 1.25

ClientFactory = Callable[[], Any]


class AnthropicProvider:
    """Streams Claude messages with tool use, prompt caching and adaptive thinking."""

    def __init__(
        self,
        name: str,
        config: ProviderConfig,
        model_overrides: Mapping[str, ModelOverride] | None = None,
        *,
        retry_delays: Sequence[float] = RETRY_DELAYS,
        sleep: Sleep = asyncio.sleep,
        http_client: Callable[[], httpx2.AsyncClient] | None = None,
    ) -> None:
        self.name = name
        self.config = config
        self._overrides = dict(model_overrides or {})
        self._retry_delays = retry_delays
        self._sleep = sleep
        self._http_client = http_client

    def capabilities(self, model: str) -> Capabilities:
        """Catalog capabilities of the model, with config overrides applied."""
        return capabilities_for(self.name, model, self._overrides)

    async def count_tokens(self, req: ChatRequest) -> int:
        """Estimate input tokens (no network call; good enough for budgeting)."""
        return estimate_tokens(req)

    def stream(self, req: ChatRequest) -> AsyncIterator[StreamItem]:
        """Stream one message, retrying rate limits, overloads and network errors."""
        return stream_with_retries(lambda: self._stream_once(req), self._retry_delays, self._sleep)

    def make_client(self) -> Any:
        """The SDK client for this provider: first-party, Bedrock (`bedrock://region`) or Vertex."""
        kwargs: dict[str, Any] = {"max_retries": 0}
        if self._http_client is not None:
            kwargs["http_client"] = self._http_client()
        url = self.config.base_url or ""
        if url.startswith("bedrock://"):
            return anthropic.AsyncAnthropicBedrockMantle(
                aws_region=url.removeprefix("bedrock://"), **kwargs
            )
        if url.startswith("vertex://"):
            project, _, region = url.removeprefix("vertex://").partition("/")
            return anthropic.AsyncAnthropicVertex(
                project_id=project, region=region or "global", **kwargs
            )
        key = os.environ.get(self.config.api_key_env) if self.config.api_key_env else None
        headers = dict(self.config.headers)
        return anthropic.AsyncAnthropic(
            api_key=key or "missing-key", base_url=url or None, default_headers=headers, **kwargs
        )

    async def _stream_once(self, req: ChatRequest) -> AsyncIterator[StreamItem]:
        client = self.make_client()
        state = StreamState()
        try:
            stream = await client.messages.create(**self.request_params(req), stream=True)
            async for event in stream:
                delta = state.feed(event)
                if delta:
                    yield StreamItem(delta=delta)
        except anthropic.APIStatusError as exc:
            raise error_from_status(
                exc.status_code, exc.response.text, exc.response.headers
            ) from exc
        except (anthropic.APIConnectionError, httpx2.HTTPError) as exc:
            raise ProviderError("network", f"{type(exc).__name__}: {exc}") from exc
        except anthropic.APIError as exc:
            raise stream_error(str(exc)) from exc
        finally:
            await client.close()
        usage = state.usage(self.capabilities(req.model))
        yield StreamItem(done=state.message(), usage=usage)

    def request_params(self, req: ChatRequest) -> dict[str, Any]:
        """The Messages API request body for a ChatRequest."""
        caps = self.capabilities(req.model)
        params: dict[str, Any] = {
            "model": req.model,
            "max_tokens": req.max_output or min(caps.max_output, DEFAULT_MAX_OUTPUT),
            "messages": to_claude_messages(req.messages),
        }
        if req.system:
            params["system"] = [
                {"type": "text", "text": req.system, "cache_control": {"type": "ephemeral"}}
            ]
        if req.tools:
            params["tools"] = [
                {"name": t.name, "description": t.description, "input_schema": t.parameters}
                for t in req.tools
            ]
        if req.temperature is not None and not caps.reasoning:
            params["temperature"] = (
                req.temperature
            )  # current Claude models reject sampling settings
        if req.reasoning_effort and caps.reasoning and "haiku" not in req.model:
            params["thinking"] = {"type": "adaptive"}
            params["output_config"] = {"effort": req.reasoning_effort}
        return params


class StreamState:
    """Collects a streamed Claude message: text, tool calls, thinking blocks and usage."""

    def __init__(self) -> None:
        self.blocks: dict[int, dict[str, Any]] = {}
        self.input_tokens = 0
        self.cache_read = 0
        self.cache_write = 0
        self.output_tokens = 0

    def feed(self, event: Any) -> str:
        """Apply one SSE event; returns text to show (empty for everything else)."""
        kind = event.type
        if kind == "message_start":
            usage = event.message.usage
            self.input_tokens = usage.input_tokens or 0
            self.cache_read = getattr(usage, "cache_read_input_tokens", 0) or 0
            self.cache_write = getattr(usage, "cache_creation_input_tokens", 0) or 0
        elif kind == "content_block_start":
            self.blocks[event.index] = event.content_block.model_dump(exclude_none=True)
            if self.blocks[event.index].get("type") == "tool_use":
                self.blocks[event.index]["partial_json"] = ""
        elif kind == "content_block_delta":
            return self._delta(self.blocks[event.index], event.delta)
        elif kind == "message_delta" and event.usage is not None:
            self.output_tokens = event.usage.output_tokens or self.output_tokens
        return ""

    def _delta(self, block: dict[str, Any], delta: Any) -> str:
        kind = delta.type
        if kind == "text_delta":
            block["text"] = block.get("text", "") + delta.text
            return str(delta.text)
        if kind == "input_json_delta":
            block["partial_json"] += delta.partial_json
        elif kind == "thinking_delta":
            block["thinking"] = block.get("thinking", "") + delta.thinking
        elif kind == "signature_delta":
            block["signature"] = delta.signature
        return ""

    def message(self) -> Message:
        """The finished assistant message."""
        ordered = [self.blocks[i] for i in sorted(self.blocks)]
        text = "".join(b.get("text", "") for b in ordered if b.get("type") == "text")
        calls = [tool_call_of(b) for b in ordered if b.get("type") == "tool_use"]
        thinking = [b for b in ordered if b.get("type") in ("thinking", "redacted_thinking")]
        parts = [TextPart(text=text)] if text else []
        return Message(
            role="assistant",
            parts=list(parts),
            tool_calls=calls,
            reasoning=json.dumps(thinking) if thinking else None,
        )

    def usage(self, caps: Capabilities) -> Usage:
        """Token counts (cached tokens included in the input) and the cost at catalog prices."""
        cost = (
            self.input_tokens * caps.cost_in
            + self.cache_read * caps.cost_in * CACHE_READ_PRICE
            + self.cache_write * caps.cost_in * CACHE_WRITE_PRICE
            + self.output_tokens * caps.cost_out
        ) / 1_000_000
        total_in = self.input_tokens + self.cache_read + self.cache_write
        return Usage(
            input_tokens=total_in,
            output_tokens=self.output_tokens,
            cached_tokens=self.cache_read,
            cost_usd=cost,
        )


def tool_call_of(block: Mapping[str, Any]) -> ToolCall:
    """A ToolCall from a finished tool_use block; unparsable input is kept raw."""
    raw = block.get("partial_json") or ""
    try:
        arguments = json.loads(raw) if raw.strip() else dict(block.get("input") or {})
    except json.JSONDecodeError:
        arguments = {"_raw_arguments": raw}
    if not isinstance(arguments, dict):
        arguments = {"_raw_arguments": raw}
    return ToolCall(id=str(block["id"]), name=str(block["name"]), arguments=arguments)


def to_claude_messages(messages: list[Message]) -> list[dict[str, Any]]:
    """Internal messages -> Claude messages; consecutive tool results share one user turn."""
    out: list[dict[str, Any]] = []
    for message in messages:
        if message.role == "system":
            continue  # the system prompt travels separately
        if message.role == "tool" and message.tool_result:
            block = tool_result_block(message)
            if out and out[-1]["role"] == "user" and out[-1].get("_tool_results"):
                out[-1]["content"].append(block)
            else:
                out.append({"role": "user", "content": [block], "_tool_results": True})
            continue
        if message.role == "assistant":
            out.append({"role": "assistant", "content": assistant_content(message)})
        else:
            out.append(
                {
                    "role": "user",
                    "content": [content_block(p) for p in message.parts]
                    or [{"type": "text", "text": "(empty)"}],
                }
            )
    for entry in out:
        entry.pop("_tool_results", None)
    return out


def assistant_content(message: Message) -> list[dict[str, Any]]:
    """Thinking blocks first (unchanged), then text, then tool calls."""
    content: list[dict[str, Any]] = json.loads(message.reasoning) if message.reasoning else []
    content += [content_block(p) for p in message.parts if isinstance(p, TextPart) and p.text]
    for call in message.tool_calls:
        arguments = {} if "_raw_arguments" in call.arguments else call.arguments
        content.append({"type": "tool_use", "id": call.id, "name": call.name, "input": arguments})
    return content or [{"type": "text", "text": "(no answer)"}]


def tool_result_block(message: Message) -> dict[str, Any]:
    """A tool_result block, with any images the tool returned."""
    result = message.tool_result
    assert result is not None
    content: list[dict[str, Any]] = [{"type": "text", "text": result.text or "(no output)"}]
    content += [content_block(image) for image in result.images]
    return {
        "type": "tool_result",
        "tool_use_id": result.call_id,
        "content": content,
        "is_error": not result.ok,
    }


def content_block(part: TextPart | ImagePart) -> dict[str, Any]:
    """A text or image content block."""
    if isinstance(part, TextPart):
        return {"type": "text", "text": part.text}
    return {
        "type": "image",
        "source": {"type": "base64", "media_type": part.media_type, "data": part.data_b64},
    }


def stream_error(text: str) -> ProviderError:
    """An error event inside the stream."""
    lowered = text.lower()
    if "overloaded" in lowered:
        return ProviderError("overloaded", text[:500])
    if "rate" in lowered and "limit" in lowered:
        return ProviderError("rate_limit", text[:500])
    if any(hint in lowered for hint in OVERFLOW_HINTS):
        return ProviderError("context_overflow", text[:500])
    return ProviderError("bad_request", text[:500])
