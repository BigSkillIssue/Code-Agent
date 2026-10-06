"""Forge configuration: one Pydantic model, loaded in layers where later layers win.

Order: built-in defaults -> ~/.forge/forge.toml -> <project>/.forge/config.toml
-> profile overlay -> FORGE_* environment variables -> CLI overrides.
"""

import logging
import os
import re
import tomllib
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, PrivateAttr, ValidationError

from forge import toml_writer
from forge.toml_writer import dumps

log = logging.getLogger(__name__)

ProviderKind = Literal["openai_compat", "anthropic", "google", "litellm"]
HookEventName = Literal[
    "session_start",
    "prompt_submit",
    "pre_tool",
    "post_tool",
    "step_done",
    "pre_compact",
    "stop",
    "subagent_stop",
]

# Keys an untrusted project may not set: they can run code or send data elsewhere.
TRUST_GATED_KEYS = ("providers", "mcp_servers", "hooks")
ENV_PREFIX = "FORGE_"
# Not config keys: where Forge keeps user files, and which shell executables to use.
RESERVED_ENV = frozenset({"FORGE_HOME", "FORGE_BASH", "FORGE_POWERSHELL"})
_SECRET_NAME = re.compile(r"key|token|secret|password|authorization|cookie", re.IGNORECASE)

DEFAULT_ROLES: dict[str, list[str]] = {
    "refiner": ["openai/gpt-5-mini"],
    "planner": ["anthropic/claude-sonnet"],
    "coder": ["anthropic/claude-sonnet", "openai/gpt-5"],
    "reviewer": ["openai/gpt-5-mini"],
    "compressor": ["openai/gpt-5-mini"],
    "explore": ["openai/gpt-5-mini"],
    "researcher": ["anthropic/claude-sonnet", "openai/gpt-5-mini"],
    "browser": ["anthropic/claude-sonnet", "openai/gpt-5"],
}


class ConfigError(Exception):
    """The configuration could not be loaded; the message names the file and the key."""


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ProviderConfig(_Strict):
    """One API endpoint Forge can talk to."""

    kind: ProviderKind = "openai_compat"
    wire: Literal["chat", "responses"] = "chat"
    base_url: str | None = None
    api_key_env: str | None = None
    headers: dict[str, str] = {}


class ModelOverride(_Strict):
    """Capability overrides for one `provider/model`; unset fields keep the catalog value."""

    context_window: int | None = None
    max_output: int | None = None
    tools: bool | None = None
    parallel_tools: bool | None = None
    vision: bool | None = None
    reasoning: bool | None = None
    prompt_cache: bool | None = None
    web_search: bool | None = None
    cost_in: float | None = None
    cost_out: float | None = None


class SandboxConfig(_Strict):
    """Where commands may write and whether they may use the network."""

    mode: Literal["read-only", "workspace-write", "full-access"] = "workspace-write"
    network: bool = False
    writable_roots: list[str] = []


class ApprovalConfig(_Strict):
    """When Forge asks the user before acting."""

    policy: Literal["on-request", "always", "never"] = "on-request"


class PermissionsConfig(_Strict):
    """Fine-grained `tool(specifier)` rules."""

    allow: list[str] = []
    ask: list[str] = []
    deny: list[str] = []


class LimitsConfig(_Strict):
    """Hard limits per step and per session."""

    max_turns_per_step: int = 40
    max_step_attempts: int = 3
    max_clarify_rounds: int = 3
    max_parallel_agents: int = 4
    max_cost_usd: float = 5.0
    compact_at: float = 0.70
    reset_at: float = 0.90
    mcp_defer_threshold: int = 40
    max_web_searches: int = 200


class WebConfig(_Strict):
    """Web search backend selection."""

    search_backend: Literal["native", "brave", "tavily", "searxng"] = "native"
    search_api_key_env: str = ""
    fallback_backend: Literal["brave", "tavily", "searxng"] | None = None


class BrowserConfig(_Strict):
    """The browser the browser agent drives (Playwright)."""

    headless: bool = True
    channel: str | None = None  # "chrome" or "msedge": an installed browser instead of Chromium
    executable: str | None = None
    viewport_width: int = 1280
    viewport_height: int = 800
    timeout_s: float = 30
    max_screenshots: int = 60


class McpServerConfig(_Strict):
    """An MCP server: a local command (stdio) or a URL (HTTP)."""

    command: list[str] | None = None
    env_keys: list[str] = []
    url: str | None = None
    headers_env: dict[str, str] = {}


class HookConfig(_Strict):
    """A shell hook; `match` is a regex on tool names (empty = every tool)."""

    match: str = ""
    command: str


class ForgeConfig(_Strict):
    """The complete Forge configuration (docs/CONTRACTS.md, Config schema)."""

    profile: str = "default"
    providers: dict[str, ProviderConfig] = {}
    models: dict[str, ModelOverride] = {}
    roles: dict[str, list[str]] = Field(default_factory=lambda: dict(DEFAULT_ROLES))
    sandbox: SandboxConfig = Field(default_factory=SandboxConfig)
    approval: ApprovalConfig = Field(default_factory=ApprovalConfig)
    permissions: PermissionsConfig = Field(default_factory=PermissionsConfig)
    limits: LimitsConfig = Field(default_factory=LimitsConfig)
    web: WebConfig = Field(default_factory=WebConfig)
    browser: BrowserConfig = Field(default_factory=BrowserConfig)
    mcp_servers: dict[str, McpServerConfig] = {}
    hooks: dict[HookEventName, list[HookConfig]] = {}
    profiles: dict[str, dict[str, Any]] = {}

    _warnings: list[str] = PrivateAttr(default_factory=list)
    _instances: dict[str, Any] = PrivateAttr(default_factory=dict)

    @property
    def warnings(self) -> list[str]:
        """Problems found while loading that did not stop it (e.g. untrusted keys)."""
        return self._warnings

    @property
    def instances(self) -> dict[str, Any]:
        """Provider objects built for this config, by name (built once, then reused)."""
        return self._instances


def forge_home() -> Path:
    """Folder for user-level Forge files: $FORGE_HOME, else ~/.forge."""
    override = os.environ.get("FORGE_HOME")
    return Path(override) if override else Path.home() / ".forge"


def find_project_root(start: Path) -> Path:
    """Nearest folder at or above `start` holding `.git` or `.forge`, else `start` itself."""
    start = start.resolve()
    for folder in (start, *start.parents):
        if (folder / ".git").exists() or (folder / ".forge").is_dir():
            return folder
    return start


def is_trusted(project_root: Path) -> bool:
    """True if `forge trust` recorded this project in ~/.forge/trusted.toml."""
    data = _read_toml(forge_home() / "trusted.toml")
    projects = data.get("projects", [])
    wanted = str(project_root.resolve())
    return any(str(Path(p).resolve()) == wanted for p in projects if isinstance(p, str))


def set_trusted(project_root: Path, trusted: bool = True) -> Path:
    """Add (or remove) the project in ~/.forge/trusted.toml; returns that file's path."""
    path = forge_home() / "trusted.toml"
    data = _read_toml(path)
    wanted = str(project_root.resolve())
    projects = [
        p
        for p in data.get("projects", [])
        if isinstance(p, str) and str(Path(p).resolve()) != wanted
    ]
    if trusted:
        projects.append(wanted)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(dumps({**data, "projects": sorted(projects)}), encoding="utf-8")
    return path


def load_config(
    project_root: Path | str,
    profile: str | None = None,
    overrides: Mapping[str, Any] | None = None,
) -> ForgeConfig:
    """Load the effective configuration for a project; raises ConfigError on bad input."""
    root = Path(project_root).resolve()
    warnings: list[str] = []
    user_path = forge_home() / "forge.toml"
    project_path = root / ".forge" / "config.toml"
    user = _checked(str(user_path), _read_toml(user_path))
    project = _read_toml(project_path)
    if project and not is_trusted(root):
        project = _strip_untrusted(project, project_path, warnings)
    project = _checked(str(project_path), project)
    env = _checked("FORGE_* environment variables", _env_layer(os.environ))
    cli = _checked("command-line options", _expand_dotted(overrides or {}))

    merged = deep_merge(deep_merge(ForgeConfig().model_dump(), user), project)
    name = profile or cli.get("profile") or env.get("profile") or merged.get("profile")
    name = str(name or "default")
    merged = _apply_profile(merged, name)
    merged = deep_merge(deep_merge(merged, env), cli)
    merged["profile"] = name
    cfg = _validated("the merged configuration", merged)
    cfg._warnings = warnings
    for warning in warnings:
        log.warning(warning)
    return cfg


def deep_merge(base: Mapping[str, Any], top: Mapping[str, Any]) -> dict[str, Any]:
    """Merge `top` over `base`: tables merge key by key, lists and values replace."""
    out = dict(base)
    for key, value in top.items():
        if isinstance(value, Mapping) and isinstance(out.get(key), Mapping):
            out[key] = deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def render_effective(cfg: ForgeConfig) -> str:
    """The configuration as TOML text, with secret values replaced by `***`."""
    return toml_writer.dumps(mask_secrets(cfg.model_dump(exclude_none=True)))


def mask_secrets(data: Any, key: str = "") -> Any:
    """Replace string values stored under secret-looking keys (not `*_env` names)."""
    if isinstance(data, Mapping):
        return {k: mask_secrets(v, str(k)) for k, v in data.items()}
    if isinstance(data, list):
        return [mask_secrets(item, key) for item in data]
    if isinstance(data, str) and data and _SECRET_NAME.search(key) and not key.endswith("_env"):
        return "***"
    return data


def _read_toml(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        return tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"invalid TOML in {path}: {exc}") from exc


def _strip_untrusted(layer: dict[str, Any], path: Path, warnings: list[str]) -> dict[str, Any]:
    cleaned = dict(layer)
    for key in TRUST_GATED_KEYS:
        if key in cleaned:
            del cleaned[key]
            warnings.append(
                f"ignored [{key}] from untrusted project config {path}; "
                "run `forge trust` in the project to allow it"
            )
    profiles = cleaned.get("profiles")
    if isinstance(profiles, Mapping):
        cleaned["profiles"] = {
            name: {k: v for k, v in body.items() if k not in TRUST_GATED_KEYS}
            for name, body in profiles.items()
            if isinstance(body, Mapping)
        }
    return cleaned


def _apply_profile(merged: dict[str, Any], name: str) -> dict[str, Any]:
    profiles = merged.get("profiles") or {}
    if name not in profiles:
        if name == "default":
            return merged
        known = ", ".join(sorted(profiles)) or "none"
        raise ConfigError(f"unknown profile '{name}' (known profiles: {known})")
    overlay = _checked(f"profile '{name}'", dict(profiles[name]))
    return deep_merge(merged, overlay)


def _env_layer(environ: Mapping[str, str]) -> dict[str, Any]:
    layer: dict[str, Any] = {}
    for name, raw in sorted(environ.items()):
        if not name.startswith(ENV_PREFIX) or name in RESERVED_ENV:
            continue
        path = [part.lower() for part in name[len(ENV_PREFIX) :].split("__")]
        _set_path(layer, path, _parse_env_value(raw))
    return layer


def _parse_env_value(raw: str) -> Any:
    """Read a TOML literal (`true`, `1.5`, `["a"]`); anything else stays a string."""
    try:
        return tomllib.loads(f"value = {raw}")["value"]
    except tomllib.TOMLDecodeError:
        return raw


def _expand_dotted(overrides: Mapping[str, Any]) -> dict[str, Any]:
    layer: dict[str, Any] = {}
    for key, value in overrides.items():
        _set_path(layer, key.split("."), value)
    return layer


def _set_path(target: dict[str, Any], path: list[str], value: Any) -> None:
    for part in path[:-1]:
        target = target.setdefault(part, {})
    if isinstance(value, Mapping) and isinstance(target.get(path[-1]), dict):
        target[path[-1]] = deep_merge(target[path[-1]], value)
    else:
        target[path[-1]] = value


def _checked(source: str, layer: dict[str, Any]) -> dict[str, Any]:
    """Validate one layer on its own so errors can name where they came from."""
    _validated(source, layer)
    return layer


def _validated(source: str, data: Mapping[str, Any]) -> ForgeConfig:
    try:
        return ForgeConfig.model_validate(data)
    except ValidationError as exc:
        raise ConfigError(_describe(source, exc)) from exc


def _describe(source: str, exc: ValidationError) -> str:
    lines = [f"invalid configuration in {source}:"]
    for error in exc.errors():
        key = ".".join(str(part) for part in error["loc"])
        if error["type"] == "extra_forbidden":
            lines.append(f"  unknown key '{key}'")
        else:
            lines.append(f"  {key}: {error['msg']} (got {error.get('input')!r})")
    return "\n".join(lines)
