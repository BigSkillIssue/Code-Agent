"""What a project's own `.forge/config.toml` may set (the trust rules of the config layers).

An untrusted project (no `forge trust`) may not set what runs code or sends data elsewhere
(providers, MCP servers, hooks, the browser it launches) or changes the release review, and may
make safety settings only stricter than the user's own: the sandbox, the approval policy, allow
rules and limits. No project, trusted or not, drops the user's deny and ask rules.
"""

from collections.abc import Mapping
from typing import Any

# Tables an untrusted project may not set at all.
TRUST_GATED_KEYS = ("providers", "mcp_servers", "hooks", "release", "browser")
# Tables an untrusted project may set only to stricter values than the user's.
STRICTER_ONLY = ("sandbox", "approval", "permissions", "limits")
SANDBOX_MODES = ("read-only", "workspace-write", "full-access")  # strictest first
APPROVAL_POLICIES = ("always", "on-request", "never")  # strictest first
KEPT_RULES = ("deny", "ask")  # a project adds to these lists and never replaces them


def restrict_untrusted(
    layer: Mapping[str, Any], base: Mapping[str, Any], source: str, warnings: list[str]
) -> dict[str, Any]:
    """The layer without gated tables and without values looser than `base` (the defaults and
    the user's own config); each dropped setting adds a warning naming `source`."""
    hint = f"from untrusted project config {source}; run `forge trust` in the project to allow it"
    cleaned: dict[str, Any] = {}
    for key, value in layer.items():
        if key in TRUST_GATED_KEYS:
            warnings.append(f"ignored [{key}] {hint}")
        elif key in STRICTER_ONLY and isinstance(value, Mapping):
            current = base.get(key)
            current = current if isinstance(current, Mapping) else {}
            kept = {k: v for k, v in value.items() if not looser(key, k, v, current.get(k))}
            warnings.extend(f"ignored {key}.{k} {hint}" for k in value if k not in kept)
            cleaned[key] = kept
        else:
            cleaned[key] = value
    return cleaned


def looser(table: str, key: str, value: Any, current: Any) -> bool:
    """True if `value` for `table.key` allows more than the user's `current` value.

    Values of the wrong type count as not looser, so validation reports them by name."""
    if table == "sandbox" and key == "mode":
        return _rank(SANDBOX_MODES, value) > _rank(SANDBOX_MODES, current)
    if table == "sandbox" and key == "network":
        return value is True and current is not True
    if table == "approval" and key == "policy":
        return _rank(APPROVAL_POLICIES, value) > _rank(APPROVAL_POLICIES, current)
    if (table, key) in (("sandbox", "writable_roots"), ("permissions", "allow")):
        allowed = current if isinstance(current, list) else []
        return isinstance(value, list) and any(item not in allowed for item in value)
    if table == "limits" and _number(value) and _number(current):
        return bool(value > current)
    return False


def keep_user_rules(layer: Mapping[str, Any], base: Mapping[str, Any]) -> dict[str, Any]:
    """The layer with the user's deny and ask rules kept in front of its own (lists replace
    each other when layers merge, so a project's list would otherwise drop them)."""
    permissions = layer.get("permissions")
    if not isinstance(permissions, Mapping):
        return dict(layer)
    users = base.get("permissions")
    users = users if isinstance(users, Mapping) else {}
    merged = dict(permissions)
    for key in KEPT_RULES:
        own, theirs = users.get(key), permissions.get(key)
        if isinstance(own, list) and isinstance(theirs, list):
            merged[key] = [*own, *(rule for rule in theirs if rule not in own)]
    return {**layer, "permissions": merged}


def _rank(order: tuple[str, ...], value: Any) -> int:
    """How loose a value is (its index); unknown values rank as the strictest."""
    return order.index(value) if value in order else -1


def _number(value: Any) -> bool:
    """An int or float that is not a bool."""
    return isinstance(value, int | float) and not isinstance(value, bool)
