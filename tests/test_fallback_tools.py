"""Prompt-based tool calling for models without native tools, and the LiteLLM adapter."""

import json
import re
import tomllib
from pathlib import Path

import pytest

from forge.config import ForgeConfig, ProviderConfig
from forge.providers.base import (
    Capabilities,
    ChatRequest,
    Message,
    ProviderError,
    ToolCall,
    ToolResult,
    ToolSpec,
    text_message,
)
from forge.providers.fake import FakeProvider, FakeTurn
from forge.providers.fallback_tools import (
    ToolFallbackProvider,
    fallback_request,
    parse_tool_calls,
    with_tool_fallback,
)
from forge.providers.litellm import LiteLLMProvider, provider_error
from forge.providers.registry import get_provider

READ = ToolSpec(
    name="read_file",
    description="Read a file",
    parameters={"type": "object", "properties": {"path": {"type": "string"}}},
)


def block(data: object) -> str:
    return "```json\n" + json.dumps(data) + "\n```"


def no_tools_fake(*turns: FakeTurn) -> FakeProvider:
    return FakeProvider(list(turns), caps=Capabilities(tools=False))


def test_parses_one_call() -> None:
    reply = "I will read it.\n" + block(
        {"tool_calls": [{"name": "read_file", "arguments": {"path": "a.py"}}]}
    )
    parsed = parse_tool_calls(reply)
    assert parsed.error is None and parsed.text == "I will read it."
    assert parsed.calls == [ToolCall(id="call_0", name="read_file", arguments={"path": "a.py"})]


def test_parses_several_calls() -> None:
    calls = [
        {"name": "read_file", "arguments": {"path": "a.py"}},
        {"name": "glob", "arguments": {"pattern": "*.py"}},
        {"name": "list_dir"},
    ]
    parsed = parse_tool_calls(block({"tool_calls": calls}))
    assert [(c.id, c.name) for c in parsed.calls] == [
        ("call_0", "read_file"),
        ("call_1", "glob"),
        ("call_2", "list_dir"),
    ]
    assert parsed.calls[2].arguments == {}


def test_plain_answer_has_no_calls() -> None:
    parsed = parse_tool_calls("The bug is fixed.")
    assert parsed.calls == [] and parsed.error is None and parsed.text == "The bug is fixed."


@pytest.mark.parametrize(
    "reply",
    [
        '```json\n{"tool_calls": [{"name": "read_file", "arguments": {"path": }]}\n```',
        block({"tool_calls": []}),
        block({"tool_calls": [{"arguments": {}}]}),
        block({"tool_calls": [{"name": "x", "arguments": "a.py"}]}),
    ],
)
def test_malformed_json_is_an_error(reply: str) -> None:
    parsed = parse_tool_calls(reply)
    assert parsed.error and parsed.calls == []


def test_request_puts_schemas_in_prompt_and_history_in_text() -> None:
    call = ToolCall(id="call_0", name="read_file", arguments={"path": "a.py"})
    history = [
        text_message("user", "fix a.py"),
        Message(role="assistant", tool_calls=[call]),
        Message(role="tool", tool_result=ToolResult(call_id="call_0", ok=True, text="x = 1")),
    ]
    req = fallback_request(ChatRequest(model="m", system="base", messages=history, tools=[READ]))
    assert req.tools == [] and req.system.startswith("base")
    assert '"read_file"' in req.system and "tool_calls" in req.system
    assert all(m.role in ("user", "assistant") for m in req.messages)
    assert '"tool_calls"' in req.messages[1].text()
    assert '"output": "x = 1"' in req.messages[2].text()


async def test_wrapper_turns_reply_into_tool_calls() -> None:
    reply = block({"tool_calls": [{"name": "read_file", "arguments": {"path": "a.py"}}]})
    fake = no_tools_fake(FakeTurn(text=reply))
    req = ChatRequest(model="m", system="s", messages=[text_message("user", "go")], tools=[READ])
    provider = with_tool_fallback(fake, req)
    assert isinstance(provider, ToolFallbackProvider)
    items = [i async for i in provider.stream(req)]
    done = items[-1].done
    assert done is not None and done.tool_calls[0].arguments == {"path": "a.py"}
    assert fake.requests[0].tools == []


async def test_malformed_reply_gets_one_error_turn() -> None:
    good = block({"tool_calls": [{"name": "read_file", "arguments": {"path": "a.py"}}]})
    fake = no_tools_fake(FakeTurn(text='```json\n{"tool_calls": [oops]}\n```'), FakeTurn(text=good))
    req = ChatRequest(model="m", system="s", messages=[text_message("user", "go")], tools=[READ])
    items = [i async for i in ToolFallbackProvider(fake).stream(req)]
    done = items[-1].done
    assert done is not None and done.tool_calls[0].name == "read_file"
    error_turn = fake.requests[1].messages[-1]
    assert error_turn.role == "user" and "invalid" in error_turn.text()


def test_native_tools_are_not_wrapped() -> None:
    fake = FakeProvider([])
    req = ChatRequest(model="m", system="s", messages=[], tools=[READ])
    assert with_tool_fallback(fake, req) is fake


async def test_litellm_streams_mock_response() -> None:
    cfg = ProviderConfig(kind="litellm")
    provider = LiteLLMProvider("litellm", cfg, extra={"mock_response": "Hello from LiteLLM"})
    req = ChatRequest(model="openai/gpt-5-mini", system="s", messages=[text_message("user", "hi")])
    items = [i async for i in provider.stream(req)]
    assert items[-1].done is not None and items[-1].done.text() == "Hello from LiteLLM"
    assert "".join(i.delta for i in items) == "Hello from LiteLLM"


def test_litellm_errors_map_to_kinds() -> None:
    import litellm

    overflow = litellm.ContextWindowExceededError("too long", model="m", llm_provider="openai")
    limited = litellm.RateLimitError("slow", model="m", llm_provider="openai")
    assert provider_error(overflow).kind == "context_overflow"
    assert provider_error(limited).kind == "rate_limit"
    assert provider_error(ValueError("x")).kind == "bad_request"
    assert isinstance(provider_error(ProviderError("auth")), ProviderError)


def test_registry_builds_litellm_preset() -> None:
    assert isinstance(get_provider("litellm", ForgeConfig()), LiteLLMProvider)


def test_providers_doc_lists_ten_providers_with_valid_snippets() -> None:
    doc = (Path(__file__).parents[1] / "docs" / "PROVIDERS.md").read_text(encoding="utf-8")
    snippets = re.findall(r"```toml\n(.*?)```", doc, re.DOTALL)
    for snippet in snippets:
        ForgeConfig.model_validate(tomllib.loads(snippet))
    providers = {p for s in snippets for p in tomllib.loads(s).get("providers", {})}
    assert len(providers) >= 10
    assert len(re.findall(r"^\| \d+ \|", doc, re.MULTILINE)) >= 10
