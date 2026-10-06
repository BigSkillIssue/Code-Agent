"""Catch-all adapter through LiteLLM (Cohere, Watsonx, Sagemaker, Cloudflare, ...).

The model id is LiteLLM's own (`cohere/command-r-plus`, `bedrock/...`). LiteLLM streams
Chat Completions-shaped chunks, so the OpenAI-compatible translation is reused. LiteLLM is
imported lazily because it is slow to import and most sessions never use it.
"""

import asyncio
import os
from collections.abc import AsyncIterator, Mapping, Sequence
from typing import Any

from forge.config import ModelOverride, ProviderConfig
from forge.providers.base import (
    Capabilities,
    ChatRequest,
    ErrorKind,
    Message,
    ProviderError,
    StreamItem,
    TextPart,
    Usage,
    cost_usd,
)
from forge.providers.catalog import capabilities_for
from forge.providers.openai_compat import (
    completed_calls,
    finish_tool_calls,
    merge_tool_call,
    parse_usage,
    to_chat_messages,
)
from forge.providers.retry import RETRY_DELAYS, Sleep, stream_with_retries
from forge.providers.tokens import estimate_tokens

# LiteLLM exception class name -> error kind (checked in order: subclasses first).
ERROR_KINDS: tuple[tuple[str, ErrorKind], ...] = (
    ("ContextWindowExceededError", "context_overflow"),
    ("RateLimitError", "rate_limit"),
    ("AuthenticationError", "auth"),
    ("PermissionDeniedError", "auth"),
    ("ServiceUnavailableError", "overloaded"),
    ("InternalServerError", "overloaded"),
    ("BadGatewayError", "overloaded"),
    ("Timeout", "network"),
    ("APIConnectionError", "network"),
)


class LiteLLMProvider:
    """Streams completions from any model LiteLLM can reach."""

    def __init__(
        self,
        name: str,
        config: ProviderConfig,
        model_overrides: Mapping[str, ModelOverride] | None = None,
        *,
        retry_delays: Sequence[float] = RETRY_DELAYS,
        sleep: Sleep = asyncio.sleep,
        extra: Mapping[str, Any] | None = None,
    ) -> None:
        self.name = name
        self.config = config
        self._overrides = dict(model_overrides or {})
        self._retry_delays = retry_delays
        self._sleep = sleep
        self._extra = dict(extra or {})  # passed to litellm.acompletion (tests use mock_response)

    def capabilities(self, model: str) -> Capabilities:
        """Catalog capabilities of the model (last path segment), with config overrides."""
        return capabilities_for(self.name, model, self._overrides)

    async def count_tokens(self, req: ChatRequest) -> int:
        """Estimate input tokens."""
        return estimate_tokens(req)

    def stream(self, req: ChatRequest) -> AsyncIterator[StreamItem]:
        """Stream one completion, retrying rate limits, overloads and network errors."""
        return stream_with_retries(lambda: self._stream_once(req), self._retry_delays, self._sleep)

    async def _stream_once(self, req: ChatRequest) -> AsyncIterator[StreamItem]:
        # LiteLLM otherwise downloads its price table on import; offline runs must not hang.
        os.environ.setdefault("LITELLM_LOCAL_MODEL_COST_MAP", "True")
        import litellm

        text: list[str] = []
        calls: dict[int, dict[str, str]] = {}
        emitted: set[int] = set()
        usage = Usage()
        try:
            stream = await litellm.acompletion(**self.request_params(req))
            async for chunk in stream:
                data = chunk.model_dump()
                if data.get("usage"):
                    usage = parse_usage(data["usage"])
                for choice in data.get("choices") or []:
                    delta = choice.get("delta") or {}
                    if delta.get("content"):
                        text.append(delta["content"])
                        yield StreamItem(delta=delta["content"])
                    for call_delta in delta.get("tool_calls") or []:
                        for call in completed_calls(calls, call_delta, emitted):
                            yield StreamItem(tool_call=call)
                        merge_tool_call(calls, call_delta)
        except Exception as exc:
            raise provider_error(exc) from exc
        parts = [TextPart(text="".join(text))] if text else []
        message = Message(role="assistant", parts=list(parts), tool_calls=finish_tool_calls(calls))
        usage.cost_usd = cost_usd(usage, self.capabilities(req.model))
        yield StreamItem(done=message, usage=usage)

    def request_params(self, req: ChatRequest) -> dict[str, Any]:
        """Keyword arguments for `litellm.acompletion`."""
        params: dict[str, Any] = {
            "model": req.model,
            "messages": to_chat_messages(req),
            "stream": True,
            "stream_options": {"include_usage": True},
            "drop_params": True,  # let LiteLLM drop settings a backend does not support
            "num_retries": 0,
            **self._extra,
        }
        if self.config.base_url:
            params["api_base"] = self.config.base_url
        if self.config.api_key_env and os.environ.get(self.config.api_key_env):
            params["api_key"] = os.environ[self.config.api_key_env]
        if self.config.headers:
            params["extra_headers"] = dict(self.config.headers)
        if req.tools:
            params["tools"] = [{"type": "function", "function": t.model_dump()} for t in req.tools]
        if req.max_output:
            params["max_tokens"] = req.max_output
        if req.temperature is not None:
            params["temperature"] = req.temperature
        if req.reasoning_effort:
            params["reasoning_effort"] = req.reasoning_effort
        return params


def provider_error(exc: Exception) -> ProviderError:
    """Map a LiteLLM (or other) exception to the shared error kinds by class name."""
    if isinstance(exc, ProviderError):
        return exc
    names = {cls.__name__ for cls in type(exc).__mro__}
    kind: ErrorKind = next((k for name, k in ERROR_KINDS if name in names), "bad_request")
    return ProviderError(kind, f"{type(exc).__name__}: {exc}"[:500])
