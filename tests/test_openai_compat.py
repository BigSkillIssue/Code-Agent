"""Offline tests for the OpenAI-compatible adapter, the registry and the FakeProvider."""

import json
from typing import Any

import httpx
import pytest
import respx

from forge.config import ForgeConfig, ModelOverride, ProviderConfig
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
from forge.providers.fake import FakeProvider, FakeTurn
from forge.providers.openai_compat import OpenAICompatProvider
from forge.providers.registry import get_provider, register_provider, resolve_role

BASE = "https://llm.test/v1"
URL = f"{BASE}/chat/completions"
NO_WAIT = (0.0, 0.0, 0.0, 0.0, 0.0)


def sse(*chunks: dict[str, Any]) -> bytes:
    body = "".join(f"data: {json.dumps(c)}\n\n" for c in chunks) + "data: [DONE]\n\n"
    return body.encode()


def chunk(delta: dict[str, Any], finish: str | None = None) -> dict[str, Any]:
    return {"choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}


def ok_stream(*chunks: dict[str, Any]) -> httpx.Response:
    return httpx.Response(200, content=sse(*chunks), headers={"content-type": "text/event-stream"})


def provider(**overrides: Any) -> OpenAICompatProvider:
    cfg = ProviderConfig(kind="openai_compat", base_url=BASE, api_key_env="TEST_LLM_KEY")
    return OpenAICompatProvider("test", cfg, retry_delays=NO_WAIT, **overrides)


def request(**fields: Any) -> ChatRequest:
    return ChatRequest(
        model="m", system="be brief", messages=[text_message("user", "hi")], **fields
    )


async def collect(p: OpenAICompatProvider, req: ChatRequest) -> list[StreamItem]:
    return [item async for item in p.stream(req)]


@respx.mock
async def test_streams_text_and_usage(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TEST_LLM_KEY", "sk-test")
    route = respx.post(URL).mock(
        return_value=ok_stream(
            chunk({"role": "assistant", "content": "Hel"}),
            chunk({"content": "lo"}, "stop"),
            {"choices": [], "usage": {"prompt_tokens": 12, "completion_tokens": 3}},
        )
    )
    items = await collect(provider(), request())
    assert [i.delta for i in items if i.delta] == ["Hel", "lo"]
    final = items[-1]
    assert final.done is not None and final.done.text() == "Hello"
    assert final.usage is not None and (final.usage.input_tokens, final.usage.output_tokens) == (
        12,
        3,
    )
    sent = json.loads(route.calls[0].request.content)
    assert sent["messages"][0] == {"role": "system", "content": "be brief"}
    assert sent["stream"] is True
    assert route.calls[0].request.headers["authorization"] == "Bearer sk-test"


@respx.mock
async def test_tool_call_split_across_chunks() -> None:
    respx.post(URL).mock(
        return_value=ok_stream(
            chunk(
                {"tool_calls": [{"index": 0, "id": "call_a", "function": {"name": "read_file"}}]}
            ),
            chunk({"tool_calls": [{"index": 0, "function": {"arguments": '{"pa'}}]}),
            chunk({"tool_calls": [{"index": 0, "function": {"arguments": 'th": "a.py"}'}}]}),
            chunk({"tool_calls": [{"index": 1, "function": {"name": "glob", "arguments": "{}"}}]}),
            chunk({}, "tool_calls"),
        )
    )
    items = await collect(
        provider(), request(tools=[ToolSpec(name="read_file", description="d", parameters={})])
    )
    done = items[-1].done
    assert done is not None
    assert done.tool_calls == [
        ToolCall(id="call_a", name="read_file", arguments={"path": "a.py"}),
        ToolCall(id="call_1", name="glob", arguments={}),
    ]
    # call_a is complete as soon as the next call starts; the last one ends with the reply
    early = [item.tool_call for item in items if item.tool_call is not None]
    assert early == done.tool_calls[:1]
    assert items.index(next(i for i in items if i.tool_call)) < len(items) - 1


@respx.mock
async def test_invalid_tool_arguments_are_kept_raw() -> None:
    respx.post(URL).mock(
        return_value=ok_stream(
            chunk(
                {
                    "tool_calls": [
                        {"index": 0, "id": "c", "function": {"name": "x", "arguments": "{oops"}}
                    ]
                }
            )
        )
    )
    items = await collect(provider(), request())
    assert items[-1].done is not None
    assert items[-1].done.tool_calls[0].arguments == {"_raw_arguments": "{oops"}


@respx.mock
async def test_rate_limit_then_success_retries() -> None:
    route = respx.post(URL)
    route.side_effect = [
        httpx.Response(429, json={"error": {"message": "slow down"}}, headers={"retry-after": "0"}),
        ok_stream(chunk({"content": "ok"}, "stop")),
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
    assert route.call_count == 6  # first try + 5 retries


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
async def test_context_overflow_is_recognised() -> None:
    respx.post(URL).mock(
        return_value=httpx.Response(
            400, json={"error": {"code": "context_length_exceeded", "message": "too long"}}
        )
    )
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
        httpx.Response(422, json={"detail": "extra fields not permitted: stream_options"}),
        ok_stream(chunk({"content": "fine"}, "stop")),
    ]
    items = await collect(provider(), request(json_schema={"type": "object"}))
    assert items[-1].done is not None and items[-1].done.text() == "fine"
    second = json.loads(route.calls[1].request.content)
    assert "stream_options" not in second and "response_format" not in second


@respx.mock
async def test_messages_are_translated() -> None:
    route = respx.post(URL).mock(return_value=ok_stream(chunk({"content": "x"}, "stop")))
    call = ToolCall(id="c1", name="read_file", arguments={"path": "a.py"})
    history = [
        text_message("user", "read a.py"),
        Message(role="assistant", parts=[TextPart(text="reading")], tool_calls=[call]),
        Message(role="tool", tool_result=ToolResult(call_id="c1", ok=True, text="1\tprint()")),
    ]
    await collect(provider(), ChatRequest(model="m", system="s", messages=history, max_output=50))
    sent = json.loads(route.calls[0].request.content)
    assert sent["messages"][2]["tool_calls"][0]["function"] == {
        "name": "read_file",
        "arguments": '{"path": "a.py"}',
    }
    assert sent["messages"][3] == {"role": "tool", "tool_call_id": "c1", "content": "1\tprint()"}
    assert sent["max_tokens"] == 50


def test_capabilities_use_config_overrides() -> None:
    p = provider(model_overrides={"test/big": ModelOverride(context_window=200_000, vision=True)})
    caps = p.capabilities("big")
    assert caps.context_window == 200_000 and caps.vision
    assert p.capabilities("other").context_window == 32_000


async def test_count_tokens_estimates_characters() -> None:
    assert await provider().count_tokens(request()) >= len("be brief" + "hi") // 4


def test_registry_builds_and_caches_providers() -> None:
    cfg = ForgeConfig(providers={"groq": ProviderConfig(base_url="https://groq.test/v1")})
    first = get_provider("groq", cfg)
    assert isinstance(first, OpenAICompatProvider)
    assert get_provider("groq", cfg) is first
    with pytest.raises(ProviderError, match="unknown provider"):
        get_provider("nope", cfg)


def test_resolve_role_splits_provider_and_model() -> None:
    cfg = ForgeConfig(
        providers={"openrouter": ProviderConfig(base_url="https://or.test/v1")},
        roles={"coder": ["openrouter/anthropic/claude-sonnet"]},
    )
    chain = resolve_role("coder", cfg)
    assert [(p.name, m) for p, m in chain] == [("openrouter", "anthropic/claude-sonnet")]
    assert resolve_role("unknown-role", cfg)[0][1] == "anthropic/claude-sonnet"


async def test_fake_provider_replays_turns_and_records_requests() -> None:
    fake = FakeProvider(
        [
            FakeTurn(
                text="Let me look.", tool_calls=[{"name": "read_file", "arguments": {"path": "a"}}]
            ),
            FakeTurn(text="Done."),
        ]
    )
    cfg = ForgeConfig(roles={"coder": ["fake/coder"]})
    register_provider(cfg, fake)
    ((prov, model),) = resolve_role("coder", cfg)
    first = [i async for i in prov.stream(ChatRequest(model=model, system="s", messages=[]))]
    assert "".join(i.delta for i in first) == "Let me look."
    assert first[-1].done is not None
    assert first[-1].done.tool_calls[0] == ToolCall(
        id="call_0", name="read_file", arguments={"path": "a"}
    )
    second = [i async for i in fake.stream(ChatRequest(model=model, system="s", messages=[]))]
    assert second[-1].done is not None and second[-1].done.text() == "Done."
    assert len(fake.requests) == 2
    third = [i async for i in fake.stream(ChatRequest(model=model, system="s", messages=[]))]
    assert third[-1].done is not None and "script exhausted" in third[-1].done.text()


async def test_fake_provider_role_queues_and_errors() -> None:
    fake = FakeProvider.from_data(
        {"roles": {"refiner": [{"text": "spec"}], "coder": [{"error": "overloaded"}]}, "turns": []}
    )
    out = [i async for i in fake.stream(ChatRequest(model="refiner", system="", messages=[]))]
    assert out[-1].done is not None and out[-1].done.text() == "spec"
    with pytest.raises(ProviderError) as exc:
        _ = [i async for i in fake.stream(ChatRequest(model="coder", system="", messages=[]))]
    assert exc.value.kind == "overloaded"
