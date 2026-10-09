"""Which model APIs the gateway reaches, which paths it lets through, and how they authenticate."""

import re
from dataclasses import dataclass
from typing import Any

from forge.providers.catalog import PRESETS

KINDS = frozenset({"anthropic", "openai_compat", "google"})
DEFAULT_BASE = {
    "anthropic": "https://api.anthropic.com",
    "google": "https://generativelanguage.googleapis.com",
}
PATHS = {
    "anthropic": re.compile(r"^v1/messages(/count_tokens)?$"),
    "openai_compat": re.compile(r"^(chat/completions|responses)$"),
    "google": re.compile(
        r"^v1(beta|alpha)?/models/([A-Za-z0-9._-]+)"
        r":(generateContent|streamGenerateContent|countTokens)$"
    ),
}
FORWARD_HEADERS = frozenset(
    {"content-type", "accept", "anthropic-version", "anthropic-beta", "openai-beta"}
)
MAX_TOKEN_FIELDS = {
    "anthropic": ("max_tokens",),
    "openai_compat": ("max_tokens", "max_completion_tokens", "max_output_tokens"),
}


@dataclass(frozen=True)
class Upstream:
    """A model API the gateway forwards to."""

    name: str
    kind: str
    base_url: str
    keyless: bool  # local servers (Ollama, LM Studio, vLLM) need no key


def upstream(name: str, overrides: dict[str, str]) -> Upstream | None:
    """The upstream for a provider name, or None if the gateway does not serve it."""
    preset = PRESETS.get(name)
    if preset is None or preset.kind not in KINDS:
        return None
    keyless = preset.api_key_env is None
    if keyless and not overrides.get(name):
        # Their presets point at localhost: that would be the server itself. Admins name where
        # such a server runs (`gateway.upstreams`) to offer it.
        return None
    base = overrides.get(name) or preset.base_url or DEFAULT_BASE[preset.kind]
    return Upstream(name, preset.kind, base.rstrip("/"), keyless=keyless)


def worker_providers(base: str, token_env: str) -> dict[str, dict[str, Any]]:
    """Forge provider configs that send every supported provider through the gateway."""
    providers = {}
    for name, preset in PRESETS.items():
        if preset.kind in KINDS:
            config = preset.model_dump()
            config.update(base_url=f"{base}/{name}", api_key_env=token_env, headers={})
            providers[name] = config
    return providers


def allowed_path(kind: str, method: str, path: str) -> bool:
    """Only model calls pass: no files, batches, fine-tuning or account endpoints."""
    return method == "POST" and bool(PATHS[kind].match(path))


def incoming_token(headers: dict[str, str], query: dict[str, str]) -> str | None:
    """The run token, wherever the provider's SDK put it."""
    authorization = headers.get("authorization", "")
    if authorization.lower().startswith("bearer "):
        return authorization[7:].strip()
    return headers.get("x-api-key") or headers.get("x-goog-api-key") or query.get("key")


def auth_headers(kind: str, key: str | None) -> dict[str, str]:
    """How the upstream expects its key."""
    if key is None:
        return {}
    if kind == "anthropic":
        return {"x-api-key": key}
    if kind == "google":
        return {"x-goog-api-key": key}
    return {"authorization": f"Bearer {key}"}


def model_of(kind: str, path: str, body: dict[str, Any]) -> str:
    """The model a request asks for."""
    if kind == "google":
        match = PATHS["google"].match(path)
        return match.group(2) if match else ""
    model = body.get("model")
    return model if isinstance(model, str) else ""


def prepare_body(kind: str, path: str, body: dict[str, Any], cap: int) -> int:
    """Cap the output tokens a request may produce (in place); returns the cap that applies.
    OpenAI-style streams are asked to report their usage at the end."""
    if kind == "google":
        config = body.setdefault("generationConfig", {})
        if isinstance(config, dict):
            config["maxOutputTokens"] = min(int(config.get("maxOutputTokens") or cap), cap)
            return int(config["maxOutputTokens"])
        return cap
    limit = cap
    for field in MAX_TOKEN_FIELDS[kind]:
        if isinstance(body.get(field), int):
            body[field] = min(body[field], cap)
            limit = body[field]
    if kind == "openai_compat" and path == "chat/completions" and body.get("stream"):
        body["stream_options"] = {"include_usage": True}
    return limit


def error_body(kind: str, status: int, message: str) -> dict[str, Any]:
    """An error in the shape the provider's SDK understands."""
    if kind == "anthropic":
        kinds = {401: "authentication_error", 403: "permission_error", 404: "not_found_error"}
        return {
            "type": "error",
            "error": {"type": kinds.get(status, "api_error"), "message": message},
        }
    if kind == "google":
        return {"error": {"code": status, "message": message, "status": "PERMISSION_DENIED"}}
    return {"error": {"message": message, "type": "forge_web_gateway", "code": status}}
