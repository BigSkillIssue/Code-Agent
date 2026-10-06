"""`forge mcp add|add-json|list|get|remove` and the `/mcp` slash command (S56)."""

import argparse
import asyncio
import shlex
from pathlib import Path

from forge.config import find_project_root
from forge.ctx import Ctx
from forge.mcp_admin import (
    Scope,
    add_request,
    add_server,
    check_server,
    describe,
    from_json,
    list_servers,
    remove_server,
)
from forge.mcp_client import McpHub

USAGE = """\
forge mcp add NAME [--scope user|project] [--env-key VAR]... -- COMMAND [ARGS...]
forge mcp add NAME [--scope user|project] --url URL [--header-env HEADER=VAR]...
forge mcp add-json NAME '{"command": "npx", "args": [...]}' [--scope user|project]
forge mcp list [--no-check]      forge mcp get NAME      forge mcp remove NAME [--scope S]"""


def cmd_mcp(options: argparse.Namespace, rest: list[str]) -> int:
    """`forge mcp ...`: manage the MCP servers in the user or project config."""
    root = find_project_root(options.cwd or Path.cwd())
    action, words = (rest[0], rest[1:]) if rest else ("", [])
    try:
        if action == "add":
            return add(root, words)
        if action == "add-json":
            return add_json(root, words)
        if action == "list":
            return asyncio.run(show_list(root, check="--no-check" not in words))
        if action == "get" and len(words) == 1:
            return get(root, words[0])
        if action == "remove" and words:
            path = remove_server(root, words[0], scope_of(words[1:]))
            print(f"removed MCP server {words[0]} from {path}")
            return 0
    except ValueError as exc:
        print(f"error: {exc}")
        return 1
    print(USAGE)
    return 2


def add(root: Path, words: list[str]) -> int:
    """`forge mcp add ...`."""
    name, scope, server = add_request(words)
    path, warnings = add_server(root, scope, name, server)
    print(f"added MCP server {name} to {path} ({describe(server)})")
    for warning in warnings:
        print(f"warning: {warning}")
    return 0


def add_json(root: Path, words: list[str]) -> int:
    """`forge mcp add-json NAME JSON [--scope S]`."""
    if len(words) < 2:
        raise ValueError("add-json needs a name and a JSON object")
    server, notes = from_json(words[1])
    path, warnings = add_server(root, scope_of(words[2:]) or "user", words[0], server)
    print(f"added MCP server {words[0]} to {path} ({describe(server)})")
    for note in [*notes, *warnings]:
        print(f"note: {note}")
    return 0


async def show_list(root: Path, check: bool) -> int:
    """`forge mcp list`: every server, where it is configured and whether it answers."""
    entries = list_servers(root)
    if not entries:
        print("no MCP servers configured; add one with: forge mcp add NAME -- COMMAND")
        return 0
    for entry in entries:
        state = await check_server(entry.name, entry.config, root) if check else ""
        print(f"{entry.name:<16} {entry.scope:<8} {describe(entry.config)}  {state}".rstrip())
    return 0


def get(root: Path, name: str) -> int:
    """`forge mcp get NAME`: the entry and where it lives."""
    for entry in list_servers(root):
        if entry.name == name:
            config = entry.config
            print(f"{name} ({entry.scope}, {entry.path})")
            print(f"  {'url' if config.url else 'command'}: {describe(config)}")
            if config.env_keys:
                print(f"  env_keys: {', '.join(config.env_keys)}")
            for header, variable in config.headers_env.items():
                print(f"  header {header} from ${variable}")
            return 0
    print(f"no MCP server '{name}'")
    return 1


def scope_of(words: list[str]) -> Scope | None:
    """The value of a `--scope` option, if given."""
    if "--scope" in words:
        index = words.index("--scope")
        if index + 1 < len(words) and words[index + 1] in ("user", "project"):
            return words[index + 1]  # type: ignore[return-value]
        raise ValueError("--scope is user or project")
    return None


async def mcp_command(ctx: Ctx, args: str) -> str:
    """/mcp, /mcp add ..., /mcp remove NAME, /mcp reconnect NAME: change servers live."""
    words = shlex.split(args)
    action, rest = (words[0], words[1:]) if words else ("", [])
    try:
        if action == "add":
            name, scope, server = add_request(rest)
            path, warnings = add_server(ctx.root, scope, name, server)
            ctx.cfg.mcp_servers[name] = server
            status = await hub_of(ctx).add_server(name, server)
            return "\n".join([status, f"saved in {path}", *warnings])
        if action == "remove" and rest:
            path = remove_server(ctx.root, rest[0], scope_of(rest[1:]))
            ctx.cfg.mcp_servers.pop(rest[0], None)
            await hub_of(ctx).remove_server(rest[0])
            return f"removed {rest[0]} (from {path})"
        if action == "reconnect" and rest:
            return await hub_of(ctx).reconnect(rest[0])
    except ValueError as exc:
        return f"error: {exc}"
    if action:
        return "usage: /mcp | /mcp add NAME ... | /mcp remove NAME | /mcp reconnect NAME"
    hub = ctx.state.mcp
    lines = hub.status() if isinstance(hub, McpHub) else []
    return "\n".join(lines) or "no MCP servers; add one with /mcp add NAME -- COMMAND"


def hub_of(ctx: Ctx) -> McpHub:
    """The session's MCP hub, created on first use."""
    if not isinstance(ctx.state.mcp, McpHub):
        ctx.state.mcp = McpHub(ctx.cfg, ctx.root)
    return ctx.state.mcp
