# Quick start

## 1. Install

```bash
uv tool install git+https://github.com/BigSkillIssue/Code-Agent
forge --version
```

Or from a checkout: `git clone ... && cd Code-Agent && uv sync`, then prefix every command
with `uv run`. You need Python 3.12+ and `git`.

## 2. Try it offline

The `--fake` flag replaces every model with a scripted one, so you can see the flow without
an API key:

```bash
forge --fake "say hello"
forge run --json --yes --fake "say hello"   # the same as JSON events, one per line
```

The repository has a seeded bug to fix offline (from a checkout):

```bash
cd examples/buggy
python -m pytest -q                                  # 1 failed
forge --fake ../../tests/fixtures/fake/fix_buggy.json "Make the tests pass"
python -m pytest -q                                  # 2 passed
git checkout -- calc.py                              # put the bug back
```

## 3. Connect a model

Set one key; the defaults use Anthropic and OpenAI models, so either works with fallback:

```bash
export ANTHROPIC_API_KEY=sk-ant-...
export OPENAI_API_KEY=sk-...
```

To choose models per role, create `~/.forge/forge.toml`:

```toml
[roles]
coder    = ["anthropic/claude-sonnet", "openai/gpt-5"]   # fallback chain
reviewer = ["openai/gpt-5-mini"]
```

Every option is in [config.md](config.md); provider snippets are in [PROVIDERS.md](PROVIDERS.md).
Check the result with `forge config check`.

## 4. Your first real task

```bash
cd your-project
forge "The date parser in src/dates.py rejects '2026-02-29'; it should reject only invalid dates"
```

What happens:

1. **Refine:** the task becomes a specification with acceptance criteria.
2. **Clarify:** up to four questions if something matters and is unclear (`/go` skips them).
3. **Plan:** steps with a check each (a command such as `pytest tests/test_dates.py`, or a
   review). You approve the plan.
4. **Execute:** each step runs until its check passes; a step that keeps failing is replanned.
5. **Report:** what changed, what was assumed, what to check by hand, and the cost.

Shell commands run in a sandbox (the project and temp folders are writable, no network);
anything else asks first. Rules in `[permissions]` change that, e.g.
`allow = ["bash(npm test*)"]`.

## 5. The terminal UI

`forge` alone opens it: the conversation on the left, the live plan on the right, a prompt
line at the bottom. Questions and approvals open dialogs. Useful commands:

| Command | Does |
| --- | --- |
| `/plan` | the plan with step statuses |
| `/undo` | roll back the last step (or file change) |
| `/go` | continue the plan |
| `/compact`, `/context` | shrink the context; show what fills it |
| `/mode read-only` | switch the sandbox or approval policy |
| `/init` | write a `FORGE.md` with the project's commands and layout |
| `/mcp` | MCP servers; `/mcp add NAME -- COMMAND` adds one without a restart |
| `/tasks` | background jobs, agents and monitors (`/tasks stop j1`); `ctrl+t` opens the list |
| `/help` | everything else, including your own commands |

## 6. Later

- `forge sessions` lists sessions; `forge resume` continues an interrupted plan.
- `forge run --json --yes "..."` for CI: exit 0 done, 1 failed, 2 needs input.
- Put project instructions in `FORGE.md` (or `AGENTS.md`/`CLAUDE.md`); Forge reads them.
- [Extending Forge](extending.md): hooks, agents, skills, commands, MCP, the Python API.
