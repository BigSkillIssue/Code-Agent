"""Offline tests for the Gemini adapter, replaying recorded streams through a mock transport."""

import json
from pathlib import Path
from typing import Any

import httpx
import pytest
from google.genai import errors

from forge.config import ForgeConfig, ProviderConfig
from forge.providers.base import (
    ChatRequest,
    Message,
    ProviderError,
    StreamItem,
    ToolCall,
    ToolResult,
    ToolSpec,
    text_message,
)
from forge.providers.google import GoogleProvider, api_error, assistant_parts
from forge.providers.registry import get_provider

FIXTURES = Path(__file__).parent / "fixtures" / "google"
NO_WAIT = (0.0, 0.0, 0.0, 0.0, 0.0)
MODEL = "gemini-2.5-flash"


def sse_response(name: str) -> httpx.Response:
    chunks = json.loads((FIXTURES / name).read_text(encoding="utf-8"))
    body = "".join(f"data: {json.dumps(c)}\r\n\r\n" for c in chunks)
    return httpx.Response(200, content=body.encode(), headers={"content-type": "text/event-stream"})


def error_response(status: int, reason: str) -> httpx.Response:
    body = {"error": {"code": status, "message": f"{reason} happened", "status": reason}}
    return httpx.Response(status, json=body)


class Recorder:
    """Answers each request with the next canned response and keeps requests."""

    def __init__(self, *responses: httpx.Response) -> None:
        self.responses = list(responses)
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self.responses.pop(0)

    def body(self, n: int) -> dict[str, Any]:
        return dict(json.loads(self.requests[n].content))


def provider(rec: Recorder) -> GoogleProvider:
    cfg = ProviderConfig(kind="google", base_url="https://gemini.test", api_key_env="TEST_GEM_KEY")
    return GoogleProvider(
        "google",
        cfg,
        retry_delays=NO_WAIT,
        http_client=lambda: httpx.AsyncClient(transport=httpx.MockTransport(rec)),
    )


def request(**fields: Any) -> ChatRequest:
    fields.setdefault("messages", [text_message("user", "hi")])
    return ChatRequest(model=MODEL, system="be brief", **fields)


async def collect(p: GoogleProvider, req: ChatRequest) -> list[StreamItem]:
    return [item async for item in p.stream(req)]


async def test_streams_text_and_usage() -> None:
    rec = Recorder(sse_response("text.json"))
    items = await collect(provider(rec), request())
    assert "".join(i.delta for i in items) == "Hello there"
    assert "streamGenerateContent" in str(rec.requests[0].url)
    body = rec.body(0)
    assert body["systemInstruction"]["parts"][0]["text"] == "be brief"
    usage = items[-1].usage
    assert usage is not None
    assert (usage.input_tokens, usage.cached_tokens, usage.output_tokens) == (2000, 1500, 10)


async def test_function_call_round_trip_keeps_thought_signature() -> None:
    rec = Recorder(sse_response("tool_call.json"), sse_response("text.json"))
    p = provider(rec)
    tools = [ToolSpec(name="read_file", description="Read a file", parameters={"type": "object"})]
    items = await collect(p, request(tools=tools, reasoning_effort="low"))
    assert "".join(i.delta for i in items) == ""  # thoughts are not shown
    done = items[-1].done
    assert done is not None and done.tool_calls[0].arguments == {"path": "calc.py"}
    body = rec.body(0)
    declared = body["tools"][0]["functionDeclarations"][0]
    assert declared["name"] == "read_file" and declared["parameters_json_schema"] == {
        "type": "object"
    }
    assert body["generationConfig"]["thinkingConfig"] == {"thinking_budget": 1024}

    call_id = done.tool_calls[0].id
    result = ToolResult(call_id=call_id, ok=True, text="def add(a, b): ...")
    history = [text_message("user", "hi"), done, Message(role="tool", tool_result=result)]
    await collect(p, request(messages=history, tools=tools))
    model_turn, tool_turn = rec.body(1)["contents"][1:]
    assert model_turn["role"] == "model"
    assert model_turn["parts"][1]["thoughtSignature"] == "c2lnLTEyMw=="
    response = tool_turn["parts"][0]["functionResponse"]
    assert response["name"] == "read_file"
    assert response["response"] == {"output": "def add(a, b): ..."}


async def test_json_schema_requests_json_output() -> None:
    rec = Recorder(sse_response("text.json"))
    await collect(provider(rec), request(json_schema={"type": "object"}))
    config = rec.body(0)["generationConfig"]
    assert config["responseMimeType"] == "application/json"


async def test_overload_is_retried() -> None:
    rec = Recorder(error_response(503, "UNAVAILABLE"), sse_response("text.json"))
    items = await collect(provider(rec), request())
    assert items[-1].done is not None and len(rec.requests) == 2


async def test_bad_key_is_auth_error() -> None:
    rec = Recorder(error_response(403, "PERMISSION_DENIED"))
    with pytest.raises(ProviderError) as info:
        await collect(provider(rec), request())
    assert info.value.kind == "auth" and len(rec.requests) == 1


def test_registry_builds_google_preset() -> None:
    assert isinstance(get_provider("gemini", ForgeConfig()), GoogleProvider)


def test_vertex_client() -> None:
    cfg = ProviderConfig(kind="google", base_url="vertex://my-project/europe-west4")
    client = GoogleProvider("v", cfg).make_client()
    assert client.vertexai


def test_rate_limit_wait_comes_from_the_body() -> None:
    """Gemini's free tier sends its wait time in the error body, not in Retry-After (live test)."""
    body = json.loads(
        (Path(__file__).parent / "fixtures" / "google" / "rate_limit_429.json").read_text()
    )
    err = api_error(errors.ClientError(429, body, None))
    assert err.kind == "rate_limit" and err.retry_after_s is not None
    assert 44 <= err.retry_after_s <= 45


def test_calls_from_other_models_get_the_dummy_thought_signature() -> None:
    """Gemini 3 refuses function calls without a signature (live run after a Groq fallback)."""
    call = ToolCall(id="c1", name="glob", arguments={"pattern": "*.py"})
    from_groq = Message(role="assistant", tool_calls=[call])
    parts = assistant_parts(from_groq)
    assert parts[0].function_call is not None
    assert parts[0].thought_signature == b"skip_thought_signature_validator"
    thinking = json.dumps([{"type": "thinking", "thinking": "hmm", "signature": "abc"}])
    from_claude = Message(role="assistant", tool_calls=[call], reasoning=thinking)
    parts = assistant_parts(from_claude)  # Claude's thinking blocks are not Gemini parts
    assert parts[0].function_call is not None and parts[0].function_call.name == "glob"
