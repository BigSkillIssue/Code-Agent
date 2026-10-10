# Forge

Forge is a coding agent for the terminal that works with any model provider. You describe a
task; Forge turns it into a precise specification (asking you when something is unclear),
plans the steps, makes the changes, checks every step with a test or a review, and ends
with a report of what changed and what it assumed.

- **Any provider:** Anthropic (also via Bedrock and Vertex), OpenAI (Chat Completions and
  Responses), Gemini, OpenRouter, Groq, DeepSeek, Mistral, xAI, Together, local models via
  Ollama, LM Studio or vLLM, and everything LiteLLM reaches. Each role (planner, coder,
  reviewer, ...) can use a different model with a fallback chain.
- **Plan first:** refine → clarify → plan → execute → verify. Steps have checks; failed
  steps are retried or replanned; a killed session resumes where it stopped.
- **Safe by default:** an OS sandbox for commands (Linux, macOS), permission rules,
  approvals, protected `.git/` and `.forge/`, secret masking, `/undo` per step.
- **Research:** a researcher sub-agent answers web questions with sources; a browser agent
  clicks through pages and sees screenshots when plain fetching is not enough
  (`forge browser install` once).
- **Teams:** sub-agents, background agents with messages, a task board and git worktrees for
  large tasks.
- **Extensible:** hooks, custom agents, skills, slash commands, MCP servers, a Python API and
  headless JSON mode, without touching Forge's code.

## Install

```bash
uv tool install git+https://github.com/BigSkillIssue/Code-Agent   # or: pipx install ...
forge --version
```

Single-file binaries for Linux, macOS and Windows are attached to each
[GitHub release](https://github.com/BigSkillIssue/Code-Agent/releases). From a checkout:
`uv sync`, then `uv run forge ...`. Python 3.12+ and `git` are required; `rg` (ripgrep) is
used when present.

## First task

```bash
export ANTHROPIC_API_KEY=...        # or OPENAI_API_KEY, GEMINI_API_KEY, ...
cd your-project
forge                               # the terminal UI
forge "Make the failing test in tests/test_calc.py pass"   # one task, plain output
forge run --json --yes "Add a --verbose flag"             # headless: JSON events, exit 0/1/2
```

No key? `forge ollama setup` runs Forge on a local model ([Ollama](https://ollama.com)), and
`forge --fake "say hello"` runs everything offline with a scripted model.
The [quick start](docs/quickstart.md) walks through a real fix step by step.

## Documentation

| Page | What it covers |
| --- | --- |
| [Quick start](docs/quickstart.md) | Install, first task, the TUI, undo, resume |
| [Configuration](docs/config.md) | Every setting (generated from the code) |
| [Providers](docs/PROVIDERS.md) | Provider setup snippets and test status |
| [Extending Forge](docs/extending.md) | Hooks, agents, skills, commands, MCP, Python API, headless, new ports |
| [Tools](docs/TOOLS.md) | Every tool the model can use, with its exact behaviour |
| [Security](docs/SECURITY.md) | Sandbox, rules, approvals, secrets, known limits |
| [Design](docs/PLAN.md), [Contracts](docs/CONTRACTS.md) | Architecture and fixed interfaces |
| [Eval results](evals/RESULTS.md) | Benchmark runs per model and prompt version |
| [Forge Web](forge-web/README.md) | Multi-user server with a web UI (separate product in `forge-web/`) |

## Commands

```text
forge                      open the terminal UI
forge "<prompt>"           work on a task
forge run --json "<p>"     headless: JSON events, exit 0 done / 1 failed / 2 needs input
forge resume [ID]          continue a session's plan
forge sessions             list this project's sessions
forge config check         print the effective configuration
forge config schema        the configuration schema (--markdown: docs/config.md)
forge trust                let this project's config set providers, MCP servers and hooks
forge eval                 run the benchmark tasks (--fake: offline)
forge browser install      download the Chromium the browser agent uses
forge mcp add NAME -- CMD  add an MCP server (also: add-json, list, get, remove)
forge ollama setup         run locally: pick, download and configure an Ollama model
forge apple new NAME       a SwiftUI app for iPhone, iPad, Mac and Apple Watch
forge app new NAME         a full-stack product: server, database, docs, tests and CI
```

Options: `-p PROFILE`, `-C DIR`, `-y/--yes` (approve everything, except an app for Apple),
`--solo`/`--team`, `--apple` (Apple guideline checks and your approval), `--fake [SCRIPT.json]`.

## Development

```bash
uv sync
uv run ruff check . && uv run mypy src && uv run pytest -q   # the gate
uv run forge --fake eval                                     # offline benchmark
```

`AGENTS.md` describes how Forge itself was built, step by step (`docs/STEPS.md`,
`PROGRESS.md`).
