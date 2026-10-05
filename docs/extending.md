# Extending Forge

Everything on this page works without changing Forge's code. `examples/plugin_demo/` shows a
hook and an MCP tool together.

## Project instructions

`FORGE.md` (also `AGENTS.md` and `CLAUDE.md`) in the project root or any folder down to the
working directory, plus `~/.forge/FORGE.md`, go into every system prompt; deeper files win.
`/init` writes a starting `FORGE.md`, and the `remember` tool adds notes to it (after asking).

## Hooks

Shell commands that run on events. The event JSON arrives on stdin; `{path}`, `{tool}` and
other names in the command are filled from it. Exit 0 continues, exit 2 blocks (stderr goes to
the model), other codes are warnings; each hook gets 30 seconds.

```toml
[hooks]
post_tool = [{ match = "edit_file|write_file", command = "ruff format {path}" }]
pre_tool  = [{ match = "bash", command = "python .forge/hooks/guard.py" }]
stop      = [{ command = "notify-send 'Forge finished'" }]
```

Events: `session_start`, `prompt_submit` (can stop a task), `pre_tool` (can block a call),
`post_tool`, `step_done`, `pre_compact`, `stop`, `subagent_stop`. Hooks in a project config
need `forge trust`.

Python hooks, for code that embeds Forge:

```python
from forge.hooks import HookOutcome, hook

@hook("pre_tool", match="bash")
def no_pushes(event, payload, ctx):
    if payload["args"].get("command", "").startswith("git push"):
        return HookOutcome(block=True, message="pushing is done by CI")
```

## Custom agents

`.forge/agents/<name>.md` (or `~/.forge/agents/`) defines a role the lead can start with
`spawn_agent`:

```markdown
---
name: api-tester
model: openai/gpt-5-mini
tools: [read_file, grep, bash]       # tool or group names; default: the coder's tools
description: Writes and runs API tests.
---
You write pytest tests for HTTP endpoints. Always run them before you report.
```

## Skills

`.forge/skills/<name>/SKILL.md` (or `~/.forge/skills/`): instructions for one kind of job.
Only the name and description go into the prompt; the agent reads the file when a task
matches.

```markdown
---
name: release
description: Cut a release - bump the version, update the changelog, tag.
---
1. Bump `__version__` ...
```

## Slash commands

`.forge/commands/<name>.md` becomes `/<name>`; `$ARGUMENTS` is replaced by what follows:

```markdown
---
description: Review code for bugs
---
Review $ARGUMENTS for bugs and report each with path:line.
```

## MCP servers

```toml
[mcp_servers.github]
command = ["npx", "-y", "@modelcontextprotocol/server-github"]
env_keys = ["GITHUB_TOKEN"]

[mcp_servers.docs]
url = "https://mcp.example.com/mcp"
headers_env = { Authorization = "DOCS_TOKEN" }
```

Tools appear as `mcp__<server>__<tool>` and ask before running; `allow =
["mcp__github__*"]` lifts that. With more than `limits.mcp_defer_threshold` tools, only their
names are listed and `tool_search` loads them on demand. `list_mcp_resources` and
`read_mcp_resource` read resources.

## Headless and CI

```bash
forge run --json --yes "Fix the lint errors"     # one JSON event per line
echo $?                                           # 0 done, 1 failed, 2 needs input (--no-defaults)
forge -p ci run --json "..."                      # with [profiles.ci] from the config
```

Without `--yes`, every approval is refused and the model is told so.

## Python API

```python
import asyncio
from forge import Forge

async def main():
    forge = Forge()                                  # config, store, executor from the project
    report = await forge.run("Fix the failing test")
    print(report.ok, report.summary)
    async for event in forge.stream("Add a --verbose flag"):
        print(event.kind)

asyncio.run(main())
```

`Forge(config, renderer, store, executor, root=..., approve=...)` replaces any port: a
renderer answers questions and approvals, a store keeps sessions elsewhere. See
`examples/embed.py`.

## New ports

The core only talks to the interfaces in `src/forge/ports.py`: `Store` (+ `BoardStore`),
`EventBus`, `Executor`, `Renderer`. A new implementation (a database store, a Docker
executor, a WebSocket renderer) must pass the suites in `tests/conformance/`; add it to the
`params` of the matching fixture. `src/forge/local/json_store.py` is a complete example.
