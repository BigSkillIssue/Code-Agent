"""Offline tests for the Claude adapter, replaying recorded SSE streams through a mock transport."""

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx2
import pytest

from forge.config import ForgeConfig, ProviderConfig
from forge.providers.anthropic import AnthropicProvider, to_claude_messages
from forge.providers.base import (
    ChatRequest,
    Message,
    ProviderError,
    StreamItem,
    ToolResult,
    ToolSpec,
    text_message,
)
from forge.providers.registry import get_provider

FIXTURES = Path(__file__).parent / "fixtures" / "anthropic"
NO_WAIT = (0.0, 0.0, 0.0, 0.0, 0.0)
Handler = Callable[[httpx2.Request], httpx2.Response]


def sse_response(name: str) -> httpx2.Response:
    events = json.loads((FIXTURES / name).read_text(encoding="utf-8"))
    body = "".join(f"event: {e['type']}\ndata: {json.dumps(e)}\n\n" for e in events)
    return httpx2.Response(
        200, content=body.encode(), headers={"content-type": "text/event-stream"}
    )


def error_response(
    status: int, kind: str, headers: dict[str, str] | None = None
) -> httpx2.Response:
    body = {"type": "error", "error": {"type": kind, "message": f"{kind} happened"}}
    return httpx2.Response(status, json=body, headers=headers or {})


class Recorder:
    """Answers each request with the next canned response and keeps the request bodies."""

    def __init__(self, *responses: httpx2.Response) -> None:
        self.responses = list(responses)
        self.bodies: list[dict[str, Any]] = []

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        self.bodies.append(json.loads(request.content))
        return self.responses.pop(0)


def provider(handler: Handler, base_url: str | None = "https://claude.test") -> AnthropicProvider:
    cfg = ProviderConfig(kind="anthropic", base_url=base_url, api_key_env="TEST_CLAUDE_KEY")
    return AnthropicProvider(
        "anthropic",
        cfg,
        retry_delays=NO_WAIT,
        http_client=lambda: httpx2.AsyncClient(transport=httpx2.MockTransport(handler)),
    )


def request(**fields: Any) -> ChatRequest:
    fields.setdefault("messages", [text_message("user", "hi")])
    return ChatRequest(model="claude-sonnet-5-5", system="be brief", **fields)


async def collect(p: AnthropicProvider, req: ChatRequest) -> list[StreamItem]:
    return [item async for item in p.stream(req)]


async def test_streams_text_and_marks_system_prompt_cacheable() -> None:
    rec = Recorder(sse_response("text.json"))
    items = await collect(provider(rec), request())
    assert "".join(i.delta for i in items) == "Hello there"
    done = items[-1].done
    assert done is not None and done.text() == "Hello there"
    assert rec.bodies[0]["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert rec.bodies[0]["stream"] is True
    usage = items[-1].usage
    assert usage is not None and usage.output_tokens == 5
    assert usage.input_tokens == 1520 and usage.cached_tokens == 0


async def test_second_call_reports_cached_tokens() -> None:
    rec = Recorder(sse_response("text.json"), sse_response("text_cached.json"))
    p = provider(rec)
    first = await collect(p, request())
    second = await collect(p, request())
    assert first[-1].usage is not None and second[-1].usage is not None
    assert second[-1].usage.cached_tokens == 1500
    assert second[-1].usage.cost_usd < first[-1].usage.cost_usd


async def test_tool_use_and_thinking_round_trip() -> None:
    rec = Recorder(sse_response("tool_use.json"), sse_response("text.json"))
    p = provider(rec)
    tools = [ToolSpec(name="read_file", description="Read a file", parameters={"type": "object"})]
    items = await collect(p, request(tools=tools, reasoning_effort="high"))
    done = items[-1].done
    assert done is not None
    assert done.tool_calls[0].name == "read_file"
    assert done.tool_calls[0].arguments == {"path": "calc.py"}
    assert [item.tool_call for item in items if item.tool_call] == done.tool_calls  # S52
    assert rec.bodies[0]["thinking"] == {"type": "adaptive"}
    assert rec.bodies[0]["output_config"] == {"effort": "high"}
    assert rec.bodies[0]["tools"][0]["input_schema"] == {"type": "object"}

    result = ToolResult(call_id="toolu_1", ok=True, text="def add(a, b): ...")
    history = [text_message("user", "hi"), done, Message(role="tool", tool_result=result)]
    await collect(p, request(messages=history, tools=tools, reasoning_effort="high"))
    assistant = rec.bodies[1]["messages"][1]
    assert assistant["content"][0] == {
        "type": "thinking",
        "thinking": "Need to read the file.",
        "signature": "sig-abc",
    }
    assert assistant["content"][1]["type"] == "tool_use"
    tool_turn = rec.bodies[1]["messages"][2]
    assert tool_turn["content"][0]["tool_use_id"] == "toolu_1"


def test_parallel_tool_results_share_one_user_turn() -> None:
    results = [
        Message(role="tool", tool_result=ToolResult(call_id=f"t{i}", ok=i == 0, text="x"))
        for i in range(2)
    ]
    messages = to_claude_messages([text_message("user", "go"), *results])
    assert len(messages) == 2
    assert [b["tool_use_id"] for b in messages[1]["content"]] == ["t0", "t1"]
    assert messages[1]["content"][1]["is_error"] is True


async def test_rate_limit_is_retried() -> None:
    rec = Recorder(
        error_response(429, "rate_limit_error", {"retry-after": "0"}), sse_response("text.json")
    )
    items = await collect(provider(rec), request())
    assert items[-1].done is not None and len(rec.bodies) == 2


async def test_bad_key_is_auth_error_without_retry() -> None:
    rec = Recorder(error_response(401, "authentication_error"))
    with pytest.raises(ProviderError) as info:
        await collect(provider(rec), request())
    assert info.value.kind == "auth" and len(rec.bodies) == 1


async def test_sampling_settings_dropped_for_reasoning_models() -> None:
    rec = Recorder(sse_response("text.json"))
    await collect(provider(rec), request(temperature=0.2))
    assert "temperature" not in rec.bodies[0]


def test_bedrock_and_vertex_use_the_sdk_clients() -> None:
    bedrock = ProviderConfig(kind="anthropic", base_url="bedrock://us-east-1")
    vertex = ProviderConfig(kind="anthropic", base_url="vertex://my-project/us-east5")
    assert "Bedrock" in type(AnthropicProvider("b", bedrock).make_client()).__name__
    assert "Vertex" in type(AnthropicProvider("v", vertex).make_client()).__name__


def test_registry_builds_anthropic_preset() -> None:
    p = get_provider("anthropic", ForgeConfig())
    assert isinstance(p, AnthropicProvider)


async def test_native_web_search_returns_results_with_cited_snippets() -> None:
    result = {"type": "web_search_result", "title": "Cats", "url": "https://c.example.com/"}
    citation = {
        "type": "web_search_result_location",
        "url": "https://c.example.com/",
        "title": "Cats",
        "encrypted_index": "y",
        "cited_text": "Cats purr.",
    }
    body = {
        "id": "msg_1",
        "type": "message",
        "role": "assistant",
        "model": "claude-sonnet-5-5",
        "content": [
            {
                "type": "server_tool_use",
                "id": "srvtoolu_1",
                "name": "web_search",
                "input": {"query": "cats"},
            },
            {
                "type": "web_search_tool_result",
                "tool_use_id": "srvtoolu_1",
                "content": [{**result, "encrypted_content": "x", "page_age": None}],
            },
            {"type": "text", "text": "Cats purr.", "citations": [citation]},
        ],
        "stop_reason": "end_turn",
        "stop_sequence": None,
        "usage": {"input_tokens": 10, "output_tokens": 5},
    }
    bodies: list[dict[str, Any]] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        bodies.append(json.loads(request.content))
        return httpx2.Response(200, json=body)

    hits = await provider(handler).search_web("claude-sonnet-5-5", "cats", 5, ["example.com"], [])
    assert hits == [("Cats", "https://c.example.com/", "Cats purr.")]
    assert bodies[0]["tools"][0]["allowed_domains"] == ["example.com"]
    assert "cats" in bodies[0]["messages"][0]["content"]
