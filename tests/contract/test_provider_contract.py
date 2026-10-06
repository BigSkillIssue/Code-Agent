"""Shared provider contract: every adapter must pass these against its live API (marked `live`).

Each case is skipped when its API key is missing (Ollama: set FORGE_LIVE_OLLAMA=1 with a
local server running). Add a row to ADAPTERS when a new adapter lands.
"""

import os

import pytest

from forge.config import ForgeConfig, ProviderConfig
from forge.providers.base import ChatRequest, Message, ToolResult, ToolSpec, text_message
from forge.providers.registry import get_provider

pytestmark = pytest.mark.live

# (provider, model, env var holding the key); every name is a preset or listed in EXTRA.
# The model `forge ollama setup` creates on a machine without a GPU; override as needed.
OLLAMA_MODEL = os.environ.get("FORGE_LIVE_OLLAMA_MODEL", "forge-qwen3:4b-instruct")

ADAPTERS = [
    ("anthropic", "claude-haiku-4-5", "ANTHROPIC_API_KEY"),
    ("openai", "gpt-5-mini", "OPENAI_API_KEY"),
    ("openai-responses", "gpt-5-mini", "OPENAI_API_KEY"),
    ("gemini", "gemini-3.8-flash", "GEMINI_API_KEY"),
    ("openrouter", "meta-llama/llama-3.3-70b-instruct:free", "OPENROUTER_API_KEY"),
    ("groq", "openai/gpt-oss-120b", "GROQ_API_KEY"),
    ("deepseek", "deepseek-chat", "DEEPSEEK_API_KEY"),
    ("mistral", "mistral-small-latest", "MISTRAL_API_KEY"),
    ("xai", "grok-4-fast", "XAI_API_KEY"),
    ("together", "meta-llama/Llama-3.3-70B-Instruct-Turbo", "TOGETHER_API_KEY"),
    ("ollama", OLLAMA_MODEL, "FORGE_LIVE_OLLAMA"),
    ("litellm", "anthropic/claude-haiku-4-5", "ANTHROPIC_API_KEY"),
]

EXTRA = {
    "openai-responses": ProviderConfig(
        base_url="https://api.openai.com/v1", api_key_env="OPENAI_API_KEY", wire="responses"
    ),
}

ADD_TOOL = ToolSpec(
    name="add",
    description="Add two integers.",
    parameters={
        "type": "object",
        "properties": {"a": {"type": "integer"}, "b": {"type": "integer"}},
        "required": ["a", "b"],
    },
)


def cases() -> list[object]:
    """One pytest param per adapter, skipped when its key is not set."""
    return [
        pytest.param(
            name,
            model,
            marks=pytest.mark.skipif(not os.environ.get(env), reason=f"{env} not set"),
            id=name,
        )
        for name, model, env in ADAPTERS
    ]


@pytest.mark.parametrize(("name", "model"), cases())
async def test_text_reply_with_usage(name: str, model: str) -> None:
    provider = get_provider(name, ForgeConfig(providers=EXTRA))
    req = ChatRequest(
        model=model, system="Answer in one word.", messages=[text_message("user", "Say hi")]
    )
    items = [item async for item in provider.stream(req)]
    assert items[-1].done is not None and items[-1].done.text().strip()
    assert items[-1].usage is not None and items[-1].usage.output_tokens > 0


@pytest.mark.parametrize(("name", "model"), cases())
async def test_tool_call_round_trip(name: str, model: str) -> None:
    provider = get_provider(name, ForgeConfig(providers=EXTRA))
    history = [text_message("user", "Use the add tool to add 2 and 3, then say the result.")]
    req = ChatRequest(model=model, system="Use tools.", messages=history, tools=[ADD_TOOL])
    first = [item async for item in provider.stream(req)][-1].done
    assert first is not None and first.tool_calls
    call = first.tool_calls[0]
    assert call.name == "add" and call.arguments == {"a": 2, "b": 3}
    result = ToolResult(call_id=call.id, ok=True, text="5")
    history += [first, Message(role="tool", tool_result=result)]
    final = [item async for item in provider.stream(req.model_copy(update={"messages": history}))]
    assert final[-1].done is not None and "5" in final[-1].done.text()
