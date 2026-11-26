"""What a request may ask for, so that its reservation is a true upper bound of its cost (SEC-W2).

Every call asks for one completion. On the server's keys a call also uses no server-side tool the
meter cannot price, cannot be routed to another model than the one priced, and refers to no input
stored at the provider (which would not be in the body the reservation is estimated from).
"""

from typing import Any

from forge_web.gateway.meter import WEB_SEARCH_USD
from forge_web.gateway.upstreams import capped

MAX_WEB_SEARCHES = 10  # per call on the server's keys (Anthropic's `max_uses`)
MULTI_CHOICE = {"openai_compat": ("n", "best_of"), "google": ("candidateCount", "candidate_count")}
# Fields that pick another model or host than the one asked for (OpenRouter, LiteLLM's proxy), add
# priced extras (web plugins, search options) or bring in input stored at the provider.
SERVER_KEY_FIELDS = {
    "anthropic": ("mcp_servers", "container"),
    "openai_compat": (
        "models", "route", "provider", "preset", "plugins", "fallbacks", "web_search_options",
        "previous_response_id", "conversation", "prompt",
    ),
    "google": ("cachedContent", "cached_content"),
}  # fmt: skip
# Anthropic's tools that run on the caller's side cost tokens only; `web_search_*` is priced.
CLIENT_TOOL_PREFIXES = ("bash_", "text_editor_", "computer_", "memory_")
OPENAI_TOOL_TYPES = frozenset({"function", "custom"})
GOOGLE_TOOL_KEYS = frozenset({"functionDeclarations", "function_declarations"})


def one_completion(kind: str, body: dict[str, Any]) -> str:
    """Why a request is refused for asking for more than one completion, or ""."""
    if kind == "google" and "generation_config" in body:
        # Google reads both spellings; the gateway caps only the one it writes.
        return "use generationConfig, not generation_config"
    where = _dict(body.get("generationConfig")) if kind == "google" else body
    for field in MULTI_CHOICE.get(kind, ()):
        value = where.get(field)
        if value is not None and (isinstance(value, bool) or value != 1):
            return f"Forge Web forwards one completion per call ({field} must be 1)"
    return ""


def server_key_refusal(kind: str, body: dict[str, Any]) -> str:
    """Why the server's keys do not pay for a request, or "" (Anthropic's web searches get a
    `max_uses` of at most MAX_WEB_SEARCHES, in place)."""
    for field in SERVER_KEY_FIELDS[kind]:
        if body.get(field) is not None:
            return (
                f"the server's keys are not used for requests with {field}; use a key of your own"
            )
    tools = body.get("tools")
    for tool in tools if isinstance(tools, list) else []:
        if not isinstance(tool, dict) or not _metered_tool(kind, tool):
            return (
                "the server's keys are not used for server-side tools Forge Web cannot price "
                f"({_tool_name(tool)}); use a key of your own"
            )
    return ""


def tool_fees(kind: str, body: dict[str, Any]) -> float:
    """The most a request's priced server-side tools can cost (USD)."""
    if kind != "anthropic" or not isinstance(body.get("tools"), list):
        return 0.0
    searches = [t for t in body["tools"] if _web_search(t)]
    return sum(int(t["max_uses"]) for t in searches) * WEB_SEARCH_USD


def _metered_tool(kind: str, tool: dict[str, Any]) -> bool:
    if kind == "openai_compat":
        return tool.get("type") in OPENAI_TOOL_TYPES
    if kind == "google":
        return set(tool) <= GOOGLE_TOOL_KEYS
    kind_of = tool.get("type")
    if kind_of is None or kind_of == "custom":
        return True
    if _web_search(tool):
        tool["max_uses"] = capped(tool.get("max_uses"), MAX_WEB_SEARCHES)
        return True
    return isinstance(kind_of, str) and kind_of.startswith(CLIENT_TOOL_PREFIXES)


def _web_search(tool: Any) -> bool:
    return isinstance(tool, dict) and str(tool.get("type") or "").startswith("web_search_")


def _tool_name(tool: Any) -> str:
    if isinstance(tool, dict):
        return str(tool.get("type") or ", ".join(map(str, tool)) or "an empty tool")
    return "a tool that is not an object"


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}
