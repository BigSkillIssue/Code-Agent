# plugin_demo

Forge extended with a hook and a tool, without changing Forge's code:

- `.forge/hooks/log_change.py` — a `post_tool` hook that logs every changed file to
  `.forge/changes.log`.
- `tools/word_count_server.py` — an MCP server; Forge offers its tool as
  `mcp__demo__word_count`.
- `.forge/config.toml` — wires both in.

Hooks and MCP servers can run code, so Forge loads them only in trusted projects:

```bash
cd examples/plugin_demo
forge trust
forge "Count the words in README.md and write the number to count.txt"
cat .forge/changes.log
```
