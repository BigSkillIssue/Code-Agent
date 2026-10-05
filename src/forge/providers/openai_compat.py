"""Adapter for every OpenAI-compatible endpoint (OpenAI, Groq, Ollama, ...).

It speaks the wire protocol over plain HTTP, so one code path serves every vendor. Each
provider picks a wire in config: Chat Completions (`chat`, default) or Responses (`responses`).
"""

import asyncio
import json
import os
from collections.abc import AsyncIterator, Mapping, Sequence
from typing import Any

import httpx

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
    cost_usd,
)
from forge.providers.catalog import capabilities_for
from forge.providers.errors import OVERFLOW_HINTS, error_from_status
from forge.providers.responses import ResponsesStream, StreamFailedError, responses_body
from forge.providers.retry import RETRY_DELAYS, Sleep, stream_with_retries
from forge.providers.sse import read_events
from forge.providers.tokens import estimate_tokens

DEFAULT_BASE_URL = "https://api.openai.com/v1"
TIMEOUT = httpx.Timeout(600.0, connect=10.0)


class OpenAICompatProvider:
    """Streams chat completions with native tool calls from an OpenAI-style API."""

    def __init__(
        self,
        name: str,
        config: ProviderConfig,
        model_overrides: Mapping[str, ModelOverride] | None = None,
        *,
        retry_delays: Sequence[float] = RETRY_DELAYS,
        sleep: Sleep = asyncio.sleep,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.name = name
        self.config = config
        self.base_url = (config.base_url or DEFAULT_BASE_URL).rstrip("/")
        self._overrides = dict(model_overrides or {})
        self._retry_delays = retry_delays
        self._sleep = sleep
        self._transport = transport

    def capabilities(self, model: str) -> Capabilities:
        """Catalog capabilities of the model, with config overrides applied."""
        return capabilities_for(self.name, model, self._overrides)

    async def count_tokens(self, req: ChatRequest) -> int:
        """Estimate input tokens (the API has no counting endpoint)."""
        return estimate_tokens(req)

    def stream(self, req: ChatRequest) -> AsyncIterator[StreamItem]:
        """Stream one completion, retrying rate limits, overloads and network errors."""
        return stream_with_retries(
            lambda: self._stream_lenient(req), self._retry_delays, self._sleep
        )

    async def _stream_lenient(self, req: ChatRequest) -> AsyncIterator[StreamItem]:
        # Many "compatible" servers reject optional fields; one retry without them is cheap.
        started = False
        try:
            async for item in self._stream_once(req, minimal=False):
                started = True
                yield item
        except ProviderError as err:
            if started or err.kind != "bad_request":
                raise
            async for item in self._stream_once(req, minimal=True):
                yield item

    async def _stream_once(self, req: ChatRequest, minimal: bool) -> AsyncIterator[StreamItem]:
        responses = self.config.wire == "responses"
        body = responses_body(req, minimal) if responses else self._body(req, minimal)
        url = f"{self.base_url}/{'responses' if responses else 'chat/completions'}"
        try:
            async with (
                httpx.AsyncClient(timeout=TIMEOUT, transport=self._transport) as client,
                client.stream("POST", url, json=body, headers=self._headers()) as response,
            ):
                if response.status_code >= 400:
                    await response.aread()
                    raise error_from_status(response.status_code, response.text, response.headers)
                parse = self._parse_responses if responses else self._parse
                async for item in parse(response, req.model):
                    yield item
        except httpx.HTTPError as exc:
            raise ProviderError("network", f"{type(exc).__name__}: {exc}") from exc

    async def _parse_responses(
        self, response: httpx.Response, model: str
    ) -> AsyncIterator[StreamItem]:
        stream = ResponsesStream()
        try:
            async for item in stream.events(response):
                yield item
        except StreamFailedError as exc:
            raise error_from_payload(exc.error) from exc
        usage = stream.usage
        usage.cost_usd = cost_usd(usage, self.capabilities(model))
        yield StreamItem(done=stream.message(), usage=usage)

    async def _parse(self, response: httpx.Response, model: str) -> AsyncIterator[StreamItem]:
        text: list[str] = []
        calls: dict[int, dict[str, str]] = {}
        usage = Usage()
        async for event in read_events(response):
            if event.data.strip() == "[DONE]":
                break
            data = json.loads(event.data)
            if data.get("error"):
                raise error_from_payload(data["error"])
            if data.get("usage"):
                usage = parse_usage(data["usage"])
            for choice in data.get("choices") or []:
                delta = choice.get("delta") or {}
                if delta.get("content"):
                    text.append(delta["content"])
                    yield StreamItem(delta=delta["content"])
                for call_delta in delta.get("tool_calls") or []:
                    merge_tool_call(calls, call_delta)
        parts = [TextPart(text="".join(text))] if text else []
        message = Message(role="assistant", parts=list(parts), tool_calls=finish_tool_calls(calls))
        usage.cost_usd = cost_usd(usage, self.capabilities(model))
        yield StreamItem(done=message, usage=usage)

    def _headers(self) -> dict[str, str]:
        headers = {"Accept": "text/event-stream", **self.config.headers}
        key = os.environ.get(self.config.api_key_env) if self.config.api_key_env else None
        if key and ".openai.azure.com" in self.base_url:
            headers["api-key"] = key
        elif key:
            headers["Authorization"] = f"Bearer {key}"
        return headers

    def _body(self, req: ChatRequest, minimal: bool) -> dict[str, Any]:
        body: dict[str, Any] = {
            "model": req.model,
            "messages": to_chat_messages(req),
            "stream": True,
        }
        if req.tools:
            body["tools"] = [{"type": "function", "function": t.model_dump()} for t in req.tools]
        if req.max_output:
            # OpenAI's own API wants the newer name; most other servers only know the old one.
            key = "max_completion_tokens" if "api.openai.com" in self.base_url else "max_tokens"
            body[key] = req.max_output
        if req.temperature is not None:
            body["temperature"] = req.temperature
        if not minimal:
            body["stream_options"] = {"include_usage": True}
            if req.reasoning_effort:
                body["reasoning_effort"] = req.reasoning_effort
            if req.json_schema:
                schema = {"name": "result", "schema": req.json_schema}
                body["response_format"] = {"type": "json_schema", "json_schema": schema}
        return body


def to_chat_messages(req: ChatRequest) -> list[dict[str, Any]]:
    """Translate the internal request into Chat Completions messages."""
    out: list[dict[str, Any]] = [{"role": "system", "content": req.system}] if req.system else []
    pending_images: list[ImagePart] = []
    for message in req.messages:
        if message.role != "tool" and pending_images:
            out.append(image_message(pending_images))
            pending_images = []
        if message.role == "tool" and message.tool_result:
            result = message.tool_result
            out.append({"role": "tool", "tool_call_id": result.call_id, "content": result.text})
            pending_images += result.images
        elif message.role == "assistant":
            out.append(assistant_message(message))
        else:
            out.append({"role": message.role, "content": content_of(message)})
    if pending_images:
        out.append(image_message(pending_images))
    return out


def assistant_message(message: Message) -> dict[str, Any]:
    """An assistant turn, including the tool calls it made."""
    entry: dict[str, Any] = {"role": "assistant", "content": message.text() or None}
    if message.tool_calls:
        entry["tool_calls"] = [
            {
                "id": call.id,
                "type": "function",
                "function": {"name": call.name, "arguments": arguments_text(call)},
            }
            for call in message.tool_calls
        ]
    return entry


def arguments_text(call: ToolCall) -> str:
    """The arguments as the model originally wrote them."""
    raw = call.arguments.get("_raw_arguments")
    return raw if isinstance(raw, str) else json.dumps(call.arguments)


def content_of(message: Message) -> str | list[dict[str, Any]]:
    """Plain text when possible, else a list of text and image parts."""
    if all(isinstance(p, TextPart) for p in message.parts):
        return message.text()
    return [part_json(p) for p in message.parts]


def part_json(part: TextPart | ImagePart) -> dict[str, Any]:
    """One content part in Chat Completions form."""
    if isinstance(part, TextPart):
        return {"type": "text", "text": part.text}
    url = f"data:{part.media_type};base64,{part.data_b64}"
    return {"type": "image_url", "image_url": {"url": url}}


def image_message(images: list[ImagePart]) -> dict[str, Any]:
    """Tool results cannot carry images in this API, so they follow as a user message."""
    parts = [{"type": "text", "text": "Images returned by the tool calls above:"}]
    return {"role": "user", "content": parts + [part_json(i) for i in images]}


def merge_tool_call(calls: dict[int, dict[str, str]], delta: Mapping[str, Any]) -> None:
    """Add one streamed tool-call fragment to the call it belongs to."""
    index = int(delta.get("index", len(calls)))
    slot = calls.setdefault(index, {"id": "", "name": "", "arguments": ""})
    if delta.get("id"):
        slot["id"] = str(delta["id"])
    function = delta.get("function") or {}
    name = function.get("name")
    if name and name != slot["name"]:
        slot["name"] += name
    arguments = function.get("arguments")
    if isinstance(arguments, Mapping):
        slot["arguments"] += json.dumps(arguments)
    elif arguments:
        slot["arguments"] += str(arguments)


def finish_tool_calls(calls: dict[int, dict[str, str]]) -> list[ToolCall]:
    """Turn the collected fragments into tool calls; unparsable arguments are kept raw."""
    result = []
    for index, slot in sorted(calls.items()):
        raw = slot["arguments"].strip() or "{}"
        try:
            arguments = json.loads(raw)
        except json.JSONDecodeError:
            arguments = None
        if not isinstance(arguments, dict):
            arguments = {"_raw_arguments": raw}
        result.append(
            ToolCall(id=slot["id"] or f"call_{index}", name=slot["name"], arguments=arguments)
        )
    return result


def parse_usage(data: Mapping[str, Any]) -> Usage:
    """Read token counts from an OpenAI-style usage object."""
    details = data.get("prompt_tokens_details") or {}
    cached = details.get("cached_tokens") or data.get("prompt_cache_hit_tokens") or 0
    return Usage(
        input_tokens=int(data.get("prompt_tokens") or 0),
        output_tokens=int(data.get("completion_tokens") or 0),
        cached_tokens=int(cached),
    )


def error_from_payload(error: Any) -> ProviderError:
    """Map an error object sent inside the event stream."""
    text = json.dumps(error).lower()
    if "rate" in text and "limit" in text:
        return ProviderError("rate_limit", text[:500])
    if "overloaded" in text or "unavailable" in text:
        return ProviderError("overloaded", text[:500])
    if any(hint in text for hint in OVERFLOW_HINTS):
        return ProviderError("context_overflow", text[:500])
    return ProviderError("bad_request", text[:500])
