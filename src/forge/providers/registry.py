"""Build providers from config and resolve a role to its (provider, model) fallback chain."""

from typing import cast

from forge.config import ForgeConfig, ProviderConfig
from forge.providers.anthropic import AnthropicProvider
from forge.providers.base import Provider, ProviderError
from forge.providers.catalog import PRESETS, canonical_model
from forge.providers.google import GoogleProvider
from forge.providers.litellm import LiteLLMProvider
from forge.providers.openai_compat import OpenAICompatProvider


def register_provider(cfg: ForgeConfig, provider: Provider) -> None:
    """Use this provider object for its name (tests and `--fake` inject providers this way)."""
    cfg.instances[provider.name] = provider


def get_provider(name: str, cfg: ForgeConfig) -> Provider:
    """The provider called `name`, built from `[providers.<name>]` once per config."""
    if name in cfg.instances:
        return cast(Provider, cfg.instances[name])
    config = cfg.providers.get(name) or PRESETS.get(name)
    if config is None:
        raise ProviderError(
            "bad_request", f"unknown provider '{name}'; add [providers.{name}] to forge.toml"
        )
    provider = build_provider(name, config, cfg)
    cfg.instances[name] = provider
    return provider


def build_provider(name: str, config: ProviderConfig, cfg: ForgeConfig) -> Provider:
    """Create the adapter for one provider entry."""
    if config.kind == "openai_compat":
        return OpenAICompatProvider(name, config, cfg.models)
    if config.kind == "anthropic":
        return AnthropicProvider(name, config, cfg.models)
    if config.kind == "google":
        return GoogleProvider(name, config, cfg.models)
    if config.kind == "litellm":
        return LiteLLMProvider(name, config, cfg.models)
    raise ProviderError("bad_request", f"provider kind '{config.kind}' is not available yet")


def resolve_role(role: str, cfg: ForgeConfig) -> list[tuple[Provider, str]]:
    """Return the (provider, model) fallback chain for a role, e.g. 'coder'."""
    chain = cfg.roles.get(role) or cfg.roles.get("coder") or []
    resolved: list[tuple[Provider, str]] = []
    for entry in chain:
        provider_name, _, model = entry.partition("/")
        if not provider_name or not model:
            raise ProviderError(
                "bad_request", f"role '{role}': '{entry}' must look like 'provider/model'"
            )
        resolved.append((get_provider(provider_name, cfg), canonical_model(model)))
    return resolved
