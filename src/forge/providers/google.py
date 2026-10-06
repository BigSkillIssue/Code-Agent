"""Adapter for Gemini: the Gemini API and Vertex AI, through the official `google-genai` SDK.

Maps tools to function declarations, `reasoning_effort` to a thinking level (or budget on
Gemini 2.5), and keeps the model's raw parts (with thought signatures) for tool-loop round trips.
"""

import asyncio
import base64
import json
import os
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from typing import Any

import httpx
from google import genai
from google.genai import errors, types

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
from forge.providers.errors import error_from_status
from forge.providers.retry import RETRY_DELAYS, Sleep, stream_with_retries
from forge.providers.tokens import estimate_tokens

DEFAULT_MAX_OUTPUT = 32_000
CACHE_READ_PRICE = 0.25  # implicit-cache hits are billed at a fraction of the input price
THINKING_BUDGETS = {"low": 1024, "medium": 8192, "high": 24576}  # Gemini 2.5 only


class GoogleProvider:
    """Streams Gemini responses with function calling, thinking and usage."""

    def __init__(
        self,
        name: str,
        config: ProviderConfig,
        model_overrides: Mapping[str, ModelOverride] | None = None,
        *,
        retry_delays: Sequence[float] = RETRY_DELAYS,
        sleep: Sleep = asyncio.sleep,
        http_client: Callable[[], httpx.AsyncClient] | None = None,
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
        """Stream one response, retrying rate limits, overloads and network errors."""
        return stream_with_retries(lambda: self._stream_once(req), self._retry_delays, self._sleep)

    def make_client(self) -> genai.Client:
        """The SDK client: Gemini API by default, Vertex AI for `vertex://<project>/<location>`."""
        options = types.HttpOptions(
            headers=dict(self.config.headers) or None,
            retry_options=types.HttpRetryOptions(attempts=1),  # retries are ours
            httpx_async_client=self._http_client() if self._http_client else None,
        )
        url = self.config.base_url or ""
        if url.startswith("vertex://"):
            project, _, location = url.removeprefix("vertex://").partition("/")
            return genai.Client(
                vertexai=True, project=project, location=location or "global", http_options=options
            )
        if url:
            options.base_url = url
        key = os.environ.get(self.config.api_key_env) if self.config.api_key_env else None
        return genai.Client(api_key=key or "missing-key", http_options=options)

    async def _stream_once(self, req: ChatRequest) -> AsyncIterator[StreamItem]:
        client = self.make_client()
        state = StreamState()
        try:
            stream = await client.aio.models.generate_content_stream(
                model=req.model,
                contents=to_gemini_contents(req.messages),
                config=self.config_for(req),
            )
            async for chunk in stream:
                seen = len(state.parts)
                delta = state.feed(chunk)
                if delta:
                    yield StreamItem(delta=delta)
                for n in range(seen, len(state.parts)):  # Gemini sends each call whole
                    if (call := state.parts[n].function_call) is not None:
                        yield StreamItem(tool_call=tool_call_of(call, n))
        except errors.APIError as exc:
            raise api_error(exc) from exc
        except httpx.HTTPError as exc:
            raise ProviderError("network", f"{type(exc).__name__}: {exc}") from exc
        finally:
            await client.aio.aclose()
        yield StreamItem(done=state.message(), usage=state.usage(self.capabilities(req.model)))

    def config_for(self, req: ChatRequest) -> types.GenerateContentConfig:
        """The generation config for a ChatRequest."""
        caps = self.capabilities(req.model)
        config = types.GenerateContentConfig(
            system_instruction=req.system or None,
            max_output_tokens=req.max_output or min(caps.max_output, DEFAULT_MAX_OUTPUT),
            temperature=req.temperature,
            # the agent loop runs tools itself; the SDK must never call Python functions
            automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
        )
        if req.tools:
            config.tools = [types.Tool(function_declarations=[declaration(t) for t in req.tools])]
        elif req.json_schema:
            config.response_mime_type = "application/json"
            config.response_json_schema = req.json_schema
        if req.reasoning_effort and caps.reasoning:
            config.thinking_config = thinking_config(req.model, req.reasoning_effort)
        return config


def declaration(tool: Any) -> types.FunctionDeclaration:
    """A function declaration from a ToolSpec (the JSON Schema is passed through)."""
    return types.FunctionDeclaration(
        name=tool.name, description=tool.description, parameters_json_schema=tool.parameters
    )


def thinking_config(model: str, effort: str) -> types.ThinkingConfig:
    """Gemini 2.5 takes a token budget; later models take a thinking level."""
    if "gemini-2.5" in model:
        return types.ThinkingConfig(thinking_budget=THINKING_BUDGETS[effort])
    level = "low" if effort == "low" else "high"
    return types.ThinkingConfig(thinking_level=types.ThinkingLevel(level.upper()))


class StreamState:
    """Collects a streamed Gemini response: parts, tool calls and the latest usage."""

    def __init__(self) -> None:
        self.parts: list[types.Part] = []
        self.usage_data: types.GenerateContentResponseUsageMetadata | None = None

    def feed(self, chunk: types.GenerateContentResponse) -> str:
        """Apply one streamed chunk; returns visible text (thoughts are not shown)."""
        if chunk.usage_metadata is not None:
            self.usage_data = chunk.usage_metadata
        content = chunk.candidates[0].content if chunk.candidates else None
        shown = ""
        for part in (content.parts if content else None) or []:
            self.parts.append(part)
            if part.text and not part.thought:
                shown += part.text
        return shown

    def message(self) -> Message:
        """The finished assistant message; raw parts are kept to send back unchanged."""
        text = "".join(p.text for p in self.parts if p.text and not p.thought)
        calls = [
            tool_call_of(p.function_call, n)
            for n, p in enumerate(self.parts)
            if p.function_call is not None
        ]
        raw = [p.model_dump(mode="json", exclude_none=True) for p in self.parts]
        return Message(
            role="assistant",
            parts=[TextPart(text=text)] if text else [],
            tool_calls=calls,
            reasoning=json.dumps(raw) if raw else None,
        )

    def usage(self, caps: Capabilities) -> Usage:
        """Token counts (thinking counts as output) and the cost at catalog prices."""
        meta = self.usage_data or types.GenerateContentResponseUsageMetadata()
        prompt = meta.prompt_token_count or 0
        cached = meta.cached_content_token_count or 0
        output = (meta.candidates_token_count or 0) + (meta.thoughts_token_count or 0)
        cost = (
            (prompt - cached) * caps.cost_in
            + cached * caps.cost_in * CACHE_READ_PRICE
            + output * caps.cost_out
        ) / 1_000_000
        return Usage(input_tokens=prompt, output_tokens=output, cached_tokens=cached, cost_usd=cost)


def tool_call_of(call: types.FunctionCall, n: int) -> ToolCall:
    """A ToolCall from a Gemini function call; ids are invented when the API sends none."""
    return ToolCall(
        id=call.id or f"call_{n}", name=call.name or "", arguments=dict(call.args or {})
    )


FOREIGN_CALL_SIGNATURE = b"skip_thought_signature_validator"


def to_gemini_contents(messages: list[Message]) -> list[types.Content]:
    """Internal messages -> Gemini contents; consecutive tool results share one user turn."""
    names: dict[str, str] = {}
    out: list[types.Content] = []
    for message in messages:
        if message.role == "system":
            continue  # the system prompt travels as system_instruction
        if message.role == "assistant":
            names.update({c.id: c.name for c in message.tool_calls})
            out.append(types.Content(role="model", parts=assistant_parts(message)))
        elif message.role == "tool" and message.tool_result:
            part = function_response(message, names)
            if out and out[-1].role == "user" and is_tool_turn(out[-1]):
                (out[-1].parts or []).append(part)
            else:
                out.append(types.Content(role="user", parts=[part]))
        else:
            parts = [content_part(p) for p in message.parts] or [types.Part(text="(empty)")]
            out.append(types.Content(role="user", parts=parts))
    return out


def is_tool_turn(content: types.Content) -> bool:
    """True when a user turn carries function responses."""
    return any(p.function_response is not None for p in content.parts or [])


def assistant_parts(message: Message) -> list[types.Part]:
    """The model's own raw parts when kept (thought signatures intact), else rebuilt ones."""
    raw = gemini_raw_parts(message.reasoning)
    if raw is not None:
        return [types.Part.model_validate(item) for item in raw]
    parts = [types.Part(text=p.text) for p in message.parts if isinstance(p, TextPart) and p.text]
    for call in message.tool_calls:
        args = {} if "_raw_arguments" in call.arguments else call.arguments
        parts.append(
            types.Part(
                function_call=types.FunctionCall(id=call.id, name=call.name, args=args),
                # Calls written by another model have no signature; Gemini 3 accepts this one.
                thought_signature=FOREIGN_CALL_SIGNATURE,
            )
        )
    return parts or [types.Part(text="(no answer)")]


def gemini_raw_parts(reasoning: str | None) -> list[dict[str, Any]] | None:
    """Kept Gemini parts; None for other vendors' reasoning (their items carry a `type`)."""
    if not reasoning:
        return None
    items = json.loads(reasoning)
    if not isinstance(items, list) or any(not isinstance(i, dict) or "type" in i for i in items):
        return None
    return items


def function_response(message: Message, names: Mapping[str, str]) -> types.Part:
    """A function_response part; Gemini matches results by function name."""
    result = message.tool_result
    assert result is not None
    key = "output" if result.ok else "error"
    response = types.FunctionResponse(
        id=result.call_id if not result.call_id.startswith("call_") else None,
        name=names.get(result.call_id, "tool"),
        response={key: result.text or "(no output)"},
    )
    return types.Part(function_response=response)


def content_part(part: TextPart | ImagePart) -> types.Part:
    """A text or inline image part."""
    if isinstance(part, TextPart):
        return types.Part(text=part.text)
    data = base64.b64decode(part.data_b64)
    return types.Part(inline_data=types.Blob(mime_type=part.media_type, data=data))


def api_error(exc: errors.APIError) -> ProviderError:
    """Map an SDK error to the shared error kinds by HTTP status."""
    response = exc.response
    headers = response.headers if isinstance(response, httpx.Response) else {}
    return error_from_status(exc.code, f"{exc.status}: {exc.message}", headers)
