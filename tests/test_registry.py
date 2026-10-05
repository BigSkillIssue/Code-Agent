"""Tests for provider presets, the model catalog and role fallback chains."""

from pathlib import Path

import pytest

from forge.agent import run_agent
from forge.config import ForgeConfig, ModelOverride, ProviderConfig
from forge.providers.base import ProviderError
from forge.providers.catalog import capabilities_for
from forge.providers.fake import FakeProvider, FakeTurn
from forge.providers.openai_compat import OpenAICompatProvider
from forge.providers.registry import get_provider, register_provider, resolve_role
from support import make_ctx


def test_changing_a_role_changes_the_model() -> None:
    cfg = ForgeConfig(roles={"coder": ["groq/llama-3.3-70b-versatile"]})
    ((provider, model),) = resolve_role("coder", cfg)
    assert isinstance(provider, OpenAICompatProvider)
    assert provider.base_url == "https://api.groq.com/openai/v1"
    assert model == "llama-3.3-70b-versatile"
    cfg.roles["coder"] = ["deepseek/deepseek-chat"]
    assert resolve_role("coder", cfg)[0][1] == "deepseek-chat"


def test_aliases_resolve_to_vendor_ids() -> None:
    cfg = ForgeConfig(
        providers={"anthropic": ProviderConfig(kind="openai_compat", base_url="https://x")},
        roles={"coder": ["anthropic/claude-sonnet"]},
    )
    assert resolve_role("coder", cfg)[0][1] == "claude-sonnet-5-5"


def test_config_entry_wins_over_preset() -> None:
    cfg = ForgeConfig(providers={"openai": ProviderConfig(base_url="https://proxy.example/v1")})
    provider = get_provider("openai", cfg)
    assert (
        isinstance(provider, OpenAICompatProvider)
        and provider.base_url == "https://proxy.example/v1"
    )


def test_unknown_model_gets_safe_defaults_and_overrides() -> None:
    caps = capabilities_for("ollama", "my-local-model")
    assert (caps.context_window, caps.cost_in) == (32_000, 0.0)
    known = capabilities_for("anthropic", "claude-sonnet")
    assert known.context_window == 1_000_000 and known.cost_in == 2.0
    tuned = capabilities_for(
        "ollama",
        "qwen3:32b",
        {"ollama/qwen3:32b": ModelOverride(context_window=32_000, tools=False)},
    )
    assert not tuned.tools


def test_unknown_provider_is_reported() -> None:
    with pytest.raises(ProviderError, match="unknown provider 'nowhere'"):
        resolve_role("coder", ForgeConfig(roles={"coder": ["nowhere/x"]}))


async def test_fallback_after_overloaded_reaches_the_second_model(tmp_project: Path) -> None:
    first = FakeProvider([FakeTurn(error="overloaded")], name="first")
    second = FakeProvider([FakeTurn(text="second answered")], name="second")
    cfg = ForgeConfig(roles={"coder": ["first/a", "second/b"]})
    register_provider(cfg, first)
    register_provider(cfg, second)
    result = await run_agent(make_ctx(tmp_project, cfg=cfg), "hi")
    assert result.text == "second answered"
    assert second.requests[0].model == "b"
