"""Add, list and remove MCP servers in the config files (`forge mcp ...`, `/mcp`), S56.

Edits are made on the TOML text: only the server's own `[mcp_servers.<name>]` tables are
replaced or removed, so comments and every other setting stay as they were.
"""

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from forge.config import McpServerConfig, forge_home, is_trusted
from forge.config_edit import parse_toml, remove_table, replace_table
from forge.mcp_client import McpConnection

Scope = Literal["user", "project"]
NAME = re.compile(r"^[A-Za-z0-9_-]{1,32}$")
ENV_REFERENCE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")
CHECK_TIMEOUT_S = 20


@dataclass
class ServerEntry:
    """A configured server and the file it comes from."""

    name: str
    scope: Scope
    path: Path
    config: McpServerConfig


def config_path(root: Path, scope: Scope) -> Path:
    """~/.forge/forge.toml for the user, .forge/config.toml for the project."""
    return forge_home() / "forge.toml" if scope == "user" else root / ".forge" / "config.toml"


def add_server(
    root: Path, scope: Scope, name: str, server: McpServerConfig
) -> tuple[Path, list[str]]:
    """Write (or replace) one server; returns the file and warnings for the user."""
    check_entry(name, server)
    path = config_path(root, scope)
    text = path.read_text(encoding="utf-8") if path.is_file() else ""
    entry = server.model_dump(exclude_defaults=True)
    updated = replace_table(text, ["mcp_servers", name], entry)
    parsed = parse_toml(updated, path)
    if parsed.get("mcp_servers", {}).get(name) != entry:
        raise ValueError(f"{path} defines {name} in a form Forge cannot edit; edit it by hand")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(updated, encoding="utf-8")
    warnings = []
    if scope == "project" and not is_trusted(root):
        warnings.append(
            "this project is not trusted yet: run `forge trust` so Forge uses its MCP servers"
        )
    return path, warnings


def remove_server(root: Path, name: str, scope: Scope | None = None) -> Path:
    """Remove a server from the project file or the user file (the first that has it)."""
    for where in [scope] if scope else ["project", "user"]:
        path = config_path(root, where)  # type: ignore[arg-type]
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8")
        if name not in parse_toml(text, path).get("mcp_servers", {}):
            continue
        updated = remove_table(text, ["mcp_servers", name])
        if name in parse_toml(updated, path).get("mcp_servers", {}):
            raise ValueError(f"{path} defines {name} in a form Forge cannot edit; edit it by hand")
        path.write_text(updated.rstrip("\n") + "\n" if updated.strip() else "", encoding="utf-8")
        return path
    raise ValueError(f"no MCP server '{name}' in the user or project config")


def list_servers(root: Path) -> list[ServerEntry]:
    """Servers from both files; a project server overrides a user server of the same name."""
    found: dict[str, ServerEntry] = {}
    scopes: tuple[Scope, ...] = ("user", "project")
    for scope in scopes:
        path = config_path(root, scope)
        if not path.is_file():
            continue
        servers = parse_toml(path.read_text(encoding="utf-8"), path).get("mcp_servers") or {}
        for name, raw in servers.items():
            found[name] = ServerEntry(name, scope, path, McpServerConfig.model_validate(raw))
    return list(found.values())


def check_entry(name: str, server: McpServerConfig) -> None:
    """A usable name, and exactly one of command and url."""
    if not NAME.match(name):
        raise ValueError(f"the name '{name}' must be 1-32 letters, digits, '-' or '_'")
    if bool(server.command) == bool(server.url):
        raise ValueError("a server needs either a command or a url (not both)")


def from_json(text: str) -> tuple[McpServerConfig, list[str]]:
    """A server entry in Claude Code / Claude Desktop JSON form -> Forge config and notes.

    Forge never stores secret values: `env` becomes `env_keys` (set the variables yourself),
    and a header that names `${VAR}` reads that variable.
    """
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError('expected a JSON object such as {"command": "npx", "args": [...]}')
    notes: list[str] = []
    if data.get("url"):
        headers: dict[str, str] = {}
        for header, value in (data.get("headers") or {}).items():
            variable = ENV_REFERENCE.search(str(value))
            if variable is None:
                notes.append(
                    f"header {header} has a fixed value; Forge reads headers from variables only"
                )
                continue
            headers[header] = variable.group(1)
            if str(value) != variable.group(0):
                notes.append(
                    f"set {variable.group(1)} to the complete {header} value (e.g. 'Bearer ...')"
                )
        return McpServerConfig(url=str(data["url"]), headers_env=headers), notes
    command = [str(data.get("command") or ""), *[str(a) for a in data.get("args") or []]]
    env = list((data.get("env") or {}).keys())
    if env:
        notes.append(f"values are not stored; set these environment variables: {', '.join(env)}")
    return McpServerConfig(command=command if command[0] else None, env_keys=env), notes


async def check_server(name: str, server: McpServerConfig, root: Path) -> str:
    """`connected, N tools` or why the server could not be reached."""
    connection = McpConnection(name, server, root)
    try:
        session = await connection.start()
        listing = await session.list_tools()
    except Exception as exc:
        return f"failed: {exc}"
    finally:
        await connection.close()
    return f"connected, {len(listing.tools)} tools"


def describe(server: McpServerConfig) -> str:
    """The command line or URL, as one line."""
    if server.url:
        return server.url
    return " ".join(server.command or [])


def add_request(words: list[str]) -> tuple[str, Scope, McpServerConfig]:
    """Parse `NAME [--scope S] [--url U] [--env-key K]... [--header-env H=VAR]... [--] CMD...`."""
    if not words:
        raise ValueError("usage: add NAME [--scope user|project] (--url URL | [--] COMMAND ...)")
    name, rest = words[0], words[1:]
    scope: str = "user"
    url: str | None = None
    env_keys: list[str] = []
    headers: dict[str, str] = {}
    command: list[str] = []
    while rest:
        word = rest.pop(0)
        if word == "--":
            command, rest = rest, []
        elif word in ("--scope", "--url", "--env-key", "--header-env"):
            if not rest:
                raise ValueError(f"{word} needs a value")
            value = rest.pop(0)
            if word == "--scope":
                scope = value
            elif word == "--url":
                url = value
            elif word == "--env-key":
                env_keys.append(value)
            else:
                header, _, variable = value.partition("=")
                if not header or not variable:
                    raise ValueError("--header-env takes HEADER=VARIABLE")
                headers[header] = variable
        else:
            command, rest = [word, *rest], []
    if scope not in ("user", "project"):
        raise ValueError("--scope is user or project")
    server = McpServerConfig(
        command=command or None, url=url, env_keys=env_keys, headers_env=headers
    )
    return name, scope, server  # type: ignore[return-value]
