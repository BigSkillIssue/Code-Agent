"""The S05 offline cases, replayed on the Responses wire (`wire = "responses"`)."""

import json
from typing import Any

import httpx
import pytest
import respx

from forge.config import ProviderConfig
from forge.providers.base import (
    ChatRequest,
    Message,
    ProviderError,
    StreamItem,
    TextPart,
    ToolCall,
    ToolResult,
    ToolSpec,
    text_message,
)
from forge.providers.openai_compat import OpenAICompatProvider

BASE = "https://llm.test/v1"
URL = f"{BASE}/responses"
NO_WAIT = (0.0, 0.0, 0.0, 0.0, 0.0)


def sse(*events: dict[str, Any]) -> bytes:
    return "".join(f"event: {e['type']}\ndata: {json.dumps(e)}\n\n" for e in events).encode()


def ok_stream(*events: dict[str, Any]) -> httpx.Response:
    return httpx.Response(200, content=sse(*events), headers={"content-type": "text/event-stream"})


def text_delta(text: str) -> dict[str, Any]:
    return {"type": "response.output_text.delta", "output_index": 0, "delta": text}


def item_done(item: dict[str, Any]) -> dict[str, Any]:
    return {"type": "response.output_item.done", "item": item}


def function_call(call_id: str, name: str, arguments: str) -> dict[str, Any]:
    item = {"type": "function_call", "id": f"fc_{call_id}", "call_id": call_id, "name": name}
    return item_done({**item, "arguments": arguments})


def completed(input_tokens: int = 0, output_tokens: int = 0, cached: int = 0) -> dict[str, Any]:
    usage = {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "input_tokens_details": {"cached_tokens": cached},
    }
    return {"type": "response.completed", "response": {"status": "completed", "usage": usage}}


def provider() -> OpenAICompatProvider:
    cfg = ProviderConfig(base_url=BASE, api_key_env="TEST_LLM_KEY", wire="responses")
    return OpenAICompatProvider("test", cfg, retry_delays=NO_WAIT)


def request(**fields: Any) -> ChatRequest:
    fields.setdefault("messages", [text_message("user", "hi")])
    return ChatRequest(model="m", system="be brief", **fields)


async def collect(p: OpenAICompatProvider, req: ChatRequest) -> list[StreamItem]:
    return [item async for item in p.stream(req)]


@respx.mock
async def test_streams_text_and_usage(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TEST_LLM_KEY", "sk-test")
    route = respx.post(URL).mock(
        return_value=ok_stream(text_delta("Hel"), text_delta("lo"), completed(12, 3, cached=8))
    )
    items = await collect(provider(), request())
    assert [i.delta for i in items if i.delta] == ["Hel", "lo"]
    final = items[-1]
    assert final.done is not None and final.done.text() == "Hello"
    assert final.usage is not None
    assert (final.usage.input_tokens, final.usage.output_tokens, final.usage.cached_tokens) == (
        12,
        3,
        8,
    )
    sent = json.loads(route.calls[0].request.content)
    assert sent["instructions"] == "be brief"
    assert sent["input"] == [{"role": "user", "content": [{"type": "input_text", "text": "hi"}]}]
    assert sent["stream"] is True and sent["store"] is False
    assert route.calls[0].request.headers["authorization"] == "Bearer sk-test"


@respx.mock
async def test_tool_calls_and_reasoning_round_trip() -> None:
    reasoning = {"type": "reasoning", "id": "rs_1", "summary": [], "encrypted_content": "enc"}
    route = respx.post(URL)
    route.side_effect = [
        ok_stream(
            item_done(reasoning),
            function_call("call_a", "read_file", '{"path": "a.py"}'),
            function_call("call_b", "glob", "{}"),
            completed(),
        ),
        ok_stream(text_delta("ok"), completed()),
    ]
    tools = [ToolSpec(name="read_file", description="d", parameters={})]
    items = await collect(provider(), request(tools=tools, reasoning_effort="high"))
    done = items[-1].done
    assert done is not None
    assert done.tool_calls == [
        ToolCall(id="call_a", name="read_file", arguments={"path": "a.py"}),
        ToolCall(id="call_b", name="glob", arguments={}),
    ]
    assert [item.tool_call for item in items if item.tool_call] == done.tool_calls  # S52
    first = json.loads(route.calls[0].request.content)
    assert first["tools"][0] == {
        "type": "function",
        "name": "read_file",
        "description": "d",
        "parameters": {},
    }
    assert first["reasoning"] == {"effort": "high"}

    result = Message(role="tool", tool_result=ToolResult(call_id="call_a", ok=True, text="x"))
    await collect(provider(), request(messages=[text_message("user", "hi"), done, result]))
    second = json.loads(route.calls[1].request.content)["input"]
    assert second[1] == reasoning
    assert second[2]["type"] == "function_call" and second[2]["call_id"] == "call_a"
    assert second[4] == {"type": "function_call_output", "call_id": "call_a", "output": "x"}


@respx.mock
async def test_invalid_tool_arguments_are_kept_raw() -> None:
    respx.post(URL).mock(return_value=ok_stream(function_call("c", "x", "{oops"), completed()))
    items = await collect(provider(), request())
    assert items[-1].done is not None
    assert items[-1].done.tool_calls[0].arguments == {"_raw_arguments": "{oops"}


@respx.mock
async def test_rate_limit_then_success_retries() -> None:
    route = respx.post(URL)
    route.side_effect = [
        httpx.Response(429, json={"error": {"message": "slow down"}}, headers={"retry-after": "0"}),
        ok_stream(text_delta("ok"), completed()),
    ]
    items = await collect(provider(), request())
    assert items[-1].done is not None and items[-1].done.text() == "ok"
    assert route.call_count == 2


@respx.mock
async def test_retries_are_exhausted() -> None:
    route = respx.post(URL).mock(return_value=httpx.Response(503, text="overloaded"))
    with pytest.raises(ProviderError) as exc:
        await collect(provider(), request())
    assert exc.value.kind == "overloaded"
    assert route.call_count == 6


@respx.mock
async def test_unauthorized_raises_auth_without_retry() -> None:
    route = respx.post(URL).mock(
        return_value=httpx.Response(401, json={"error": {"message": "bad key"}})
    )
    with pytest.raises(ProviderError) as exc:
        await collect(provider(), request())
    assert exc.value.kind == "auth"
    assert route.call_count == 1


@respx.mock
async def test_context_overflow_in_stream_is_recognised() -> None:
    failed = {
        "type": "response.failed",
        "response": {"error": {"code": "context_length_exceeded", "message": "too long"}},
    }
    respx.post(URL).mock(return_value=ok_stream(failed))
    with pytest.raises(ProviderError) as exc:
        await collect(provider(), request())
    assert exc.value.kind == "context_overflow"


@respx.mock
async def test_network_error_is_retried_then_raised() -> None:
    route = respx.post(URL).mock(side_effect=httpx.ConnectError("refused"))
    with pytest.raises(ProviderError) as exc:
        await collect(provider(), request())
    assert exc.value.kind == "network"
    assert route.call_count == 6


@respx.mock
async def test_bad_request_retries_once_with_minimal_body() -> None:
    route = respx.post(URL)
    route.side_effect = [
        httpx.Response(422, json={"detail": "unknown field: include"}),
        ok_stream(text_delta("fine"), completed()),
    ]
    items = await collect(provider(), request(json_schema={"type": "object"}))
    assert items[-1].done is not None and items[-1].done.text() == "fine"
    first = json.loads(route.calls[0].request.content)
    assert first["text"]["format"]["type"] == "json_schema"
    second = json.loads(route.calls[1].request.content)
    assert "include" not in second and "text" not in second and "store" not in second


@respx.mock
async def test_messages_are_translated() -> None:
    route = respx.post(URL).mock(return_value=ok_stream(text_delta("x"), completed()))
    call = ToolCall(id="c1", name="read_file", arguments={"path": "a.py"})
    history = [
        text_message("user", "read a.py"),
        Message(role="assistant", parts=[TextPart(text="reading")], tool_calls=[call]),
        Message(role="tool", tool_result=ToolResult(call_id="c1", ok=True, text="1\tprint()")),
    ]
    await collect(provider(), ChatRequest(model="m", system="s", messages=history, max_output=50))
    sent = json.loads(route.calls[0].request.content)
    assert sent["input"][1] == {"role": "assistant", "content": "reading"}
    assert sent["input"][2] == {
        "type": "function_call",
        "call_id": "c1",
        "name": "read_file",
        "arguments": '{"path": "a.py"}',
    }
    assert sent["input"][3]["output"] == "1\tprint()"
    assert sent["max_output_tokens"] == 50


@respx.mock
async def test_chat_wire_is_still_the_default() -> None:
    route = respx.post(f"{BASE}/chat/completions").mock(
        return_value=httpx.Response(
            200, content=b"data: [DONE]\n\n", headers={"content-type": "text/event-stream"}
        )
    )
    cfg = ProviderConfig(base_url=BASE)
    await collect(OpenAICompatProvider("t", cfg, retry_delays=NO_WAIT), request())
    assert route.call_count == 1
