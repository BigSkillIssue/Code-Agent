"""Known providers and models: zero-config presets, capabilities, prices and aliases.

Prices are USD per 1M tokens as published by each vendor when this table was written;
config `[models."<provider>/<model>"]` entries override any value, and unknown models get
safe defaults.
"""

from forge.config import ModelOverride, ProviderConfig
from forge.providers.base import Capabilities

# Provider presets: used when a role names a provider that has no [providers.<name>] entry.
PRESETS: dict[str, ProviderConfig] = {
    "openai": ProviderConfig(
        kind="openai_compat", base_url="https://api.openai.com/v1", api_key_env="OPENAI_API_KEY"
    ),
    "anthropic": ProviderConfig(kind="anthropic", api_key_env="ANTHROPIC_API_KEY"),
    "google": ProviderConfig(kind="google", api_key_env="GEMINI_API_KEY"),
    "gemini": ProviderConfig(kind="google", api_key_env="GEMINI_API_KEY"),
    "openrouter": ProviderConfig(
        kind="openai_compat",
        base_url="https://openrouter.ai/api/v1",
        api_key_env="OPENROUTER_API_KEY",
    ),
    "groq": ProviderConfig(
        kind="openai_compat", base_url="https://api.groq.com/openai/v1", api_key_env="GROQ_API_KEY"
    ),
    "deepseek": ProviderConfig(
        kind="openai_compat", base_url="https://api.deepseek.com/v1", api_key_env="DEEPSEEK_API_KEY"
    ),
    "mistral": ProviderConfig(
        kind="openai_compat", base_url="https://api.mistral.ai/v1", api_key_env="MISTRAL_API_KEY"
    ),
    "xai": ProviderConfig(
        kind="openai_compat", base_url="https://api.x.ai/v1", api_key_env="XAI_API_KEY"
    ),
    "together": ProviderConfig(
        kind="openai_compat", base_url="https://api.together.xyz/v1", api_key_env="TOGETHER_API_KEY"
    ),
    "fireworks": ProviderConfig(
        kind="openai_compat",
        base_url="https://api.fireworks.ai/inference/v1",
        api_key_env="FIREWORKS_API_KEY",
    ),
    "perplexity": ProviderConfig(
        kind="openai_compat", base_url="https://api.perplexity.ai", api_key_env="PERPLEXITY_API_KEY"
    ),
    "cerebras": ProviderConfig(
        kind="openai_compat", base_url="https://api.cerebras.ai/v1", api_key_env="CEREBRAS_API_KEY"
    ),
    "ollama": ProviderConfig(kind="openai_compat", base_url="http://localhost:11434/v1"),
    "lmstudio": ProviderConfig(kind="openai_compat", base_url="http://localhost:1234/v1"),
    "vllm": ProviderConfig(kind="openai_compat", base_url="http://localhost:8000/v1"),
    "litellm": ProviderConfig(kind="litellm"),
}

# Short names people use in config, mapped to the vendor's model id.
ALIASES: dict[str, str] = {
    "claude-opus": "claude-opus-5-5",
    "claude-sonnet": "claude-sonnet-5-5",
    "claude-haiku": "claude-haiku-4-5",
    "claude-fable": "claude-fable-5-1",
    "gemini-pro": "gemini-2.5-pro",
    "gemini-flash": "gemini-2.5-flash",
}


def _claude(context: int, output: int, cost_in: float, cost_out: float) -> Capabilities:
    return Capabilities(
        context_window=context,
        max_output=output,
        tools=True,
        parallel_tools=True,
        vision=True,
        reasoning=True,
        prompt_cache=True,
        web_search=True,
        cost_in=cost_in,
        cost_out=cost_out,
    )


def _general(
    context: int, output: int, cost_in: float, cost_out: float, *, cache: bool = True
) -> Capabilities:
    return Capabilities(
        context_window=context,
        max_output=output,
        tools=True,
        parallel_tools=True,
        vision=True,
        reasoning=True,
        prompt_cache=cache,
        cost_in=cost_in,
        cost_out=cost_out,
    )


KNOWN_MODELS: dict[str, Capabilities] = {
    "claude-fable-5-1": _claude(1_000_000, 128_000, 10.0, 50.0),
    "claude-opus-5-5": _claude(1_000_000, 128_000, 4.0, 20.0),
    "claude-opus-5": _claude(1_000_000, 128_000, 5.0, 25.0),
    "claude-sonnet-5-5": _claude(1_000_000, 128_000, 2.0, 10.0),
    "claude-sonnet-5": _claude(1_000_000, 128_000, 2.0, 10.0),
    "claude-sonnet-4-6": _claude(1_000_000, 128_000, 3.0, 15.0),
    "claude-haiku-4-5": _claude(200_000, 64_000, 1.0, 5.0),
    "gpt-5": _general(400_000, 128_000, 1.25, 10.0),
    "gpt-5-mini": _general(400_000, 128_000, 0.25, 2.0),
    "gpt-5-nano": _general(400_000, 128_000, 0.05, 0.40),
    "gpt-4.1": _general(1_000_000, 32_768, 2.0, 8.0),
    "gemini-2.5-pro": _general(1_000_000, 65_536, 1.25, 10.0),
    "gemini-2.5-flash": _general(1_000_000, 65_536, 0.30, 2.50),
    "deepseek-chat": _general(128_000, 8_192, 0.27, 1.10),
    "llama-3.3-70b-versatile": _general(128_000, 32_768, 0.59, 0.79, cache=False),
}


def canonical_model(model: str) -> str:
    """The vendor's model id for an alias such as 'claude-sonnet'."""
    return ALIASES.get(model, model)


def capabilities_for(
    provider: str, model: str, overrides: dict[str, ModelOverride] | None = None
) -> Capabilities:
    """Capabilities of `provider/model`: catalog entry (or safe defaults) plus overrides."""
    model = canonical_model(model)
    caps = KNOWN_MODELS.get(model) or KNOWN_MODELS.get(model.rsplit("/", 1)[-1]) or Capabilities()
    override = (overrides or {}).get(f"{provider}/{model}")
    if override is None:
        return caps
    changes = {k: v for k, v in override.model_dump().items() if v is not None}
    return caps.model_copy(update=changes)
