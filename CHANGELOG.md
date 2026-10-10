# Changelog

All notable changes to Forge. Versions follow [Semantic Versioning](https://semver.org).

## Unreleased

Phase 6 (S49-S56), after v1.0:

- **Research agent:** the `research` tool hands web questions to a researcher sub-agent that
  answers with sources (also in solo mode); the coder and lead prompts prefer it for anything
  beyond a single fact. `web.fallback_backend` covers models without their own search.
- **Browser agent:** `research(browser=true)` starts a browser sub-agent that opens pages,
  clicks, types and scrolls, and sees a screenshot after every action (Playwright; run
  `forge browser install` once, or use an installed Chrome with `[browser] channel`). Local and
  private addresses are blocked.
- **Live tool output:** shell commands show their output while they run (`tool_output` events).
- **Early tool start:** safe reads start while the model is still writing its reply.
- **Monitor tool:** `monitor` runs a command in the background and sends each new (filtered)
  output line to the agent as a message; the agent waits for them.
- **Todo list:** `todo_write` keeps the agent's checklist, shown live in the TUI.
- **Background tasks:** `/tasks` and `ctrl+t` list jobs, agents and monitors, with output and stop.
- **Local models:** `forge ollama setup` picks a model for the machine, downloads it, gives it a
  big enough context window and points every role at it; `forge ollama status` checks it.
- **MCP management:** `forge mcp add/add-json/list/get/remove` and `/mcp` (add, remove,
  reconnect without a restart).

Phase 7 (S58-S60), Apple apps:

- **Apple builds:** on a Mac with Xcode, `apple_build` builds, tests and archives (unsigned) for
  iOS, iPadOS, macOS and watchOS, and `apple_screenshot` shows the app on a simulated iPhone,
  iPad or Apple Watch, or on the Mac, in light or dark mode. A server can offer the same with a
  remote Mac through the `AppleBuilder` port.
- **Apple app template:** `forge apple new NAME` creates a SwiftUI app for iPhone, iPad, Mac and
  Apple Watch: an XcodeGen `project.yml`, shared code, unit tests, a privacy manifest and the
  Mac App Store sandbox. CI builds, tests, archives and photographs it with a real Xcode.
- **Apple guideline reviewer:** an independent `apple_reviewer` (its own model, a fresh
  context, read-only tools and Apple's current pages) judges a request, a plan or a finished app
  per guideline area: ok, concern or violation, with the guideline's number and a fix.
- **`--apple`:** the request is reviewed before planning, the plan before building and the app
  once it builds, passes its tests and has been photographed on every device in light and dark
  mode. Violations go back to the planner or the coder; what they cannot fix, you decide. Only an
  app you approve is reported as ready for Apple.

Phase 8 (Produkt-Fabrik: complete products, hosted by Forge Web):

- **App manifest:** `forge.app.toml` describes a product for hosting (services, database,
  storage, mail, secret names, clients, payments); every problem is reported with its field.
- **Full-stack template:** `forge app new NAME` creates a product people can keep working on:
  a FastAPI server on PostgreSQL 16 with accounts (password reset, account deletion, data
  export), content reports and blocking, rate limits, Alembic migrations, an OpenAPI snapshot,
  tests, a runbook and CI. CI's `fullstack` job installs it and runs its tests on PostgreSQL.

## 1.0.0 - 2026-10-05

The first release: everything planned in `docs/STEPS.md` (S01-S48).

### Agent and pipeline
- Refine → clarify → plan → execute → verify pipeline; steps have checks, failing steps are
  retried or replanned, and a killed session resumes from its plan (`forge resume`).
- `/undo` per step or file change, context compaction and reset, budgets per session.
- Solo and team modes: sub-agents, background agents with messages, a shared task board and
  git worktrees that are merged back after review.

### Providers
- Anthropic (also Bedrock and Vertex), OpenAI Chat Completions and Responses, Gemini, every
  OpenAI-compatible endpoint (OpenRouter, Groq, DeepSeek, Mistral, xAI, Together, Ollama,
  LM Studio, vLLM) and LiteLLM as a catch-all.
- Fallback chains per role, retries, prompt caching, prompt-based tools for models without
  native tool calling.

### Tools
- File tools, `apply_patch`, `grep`, `glob`, `repo_map`, shells (bash and PowerShell, persistent
  sessions, background jobs), `web_fetch`, `web_search`, memory, team and MCP tools.

### Safety
- OS sandbox for commands (Landlock or bubblewrap on Linux, Seatbelt on macOS), permission
  rules that see through chained and wrapped commands, approvals, protected `.git/` and
  `.forge/`, secret masking, `forge trust` for project configs.

### Interfaces and extensions
- Terminal UI (Textual), plain CLI, headless JSON mode with exit codes, Python API (`Forge`).
- Hooks (shell and Python), custom agents, skills, slash commands, MCP servers.
- Ports for stores, event buses, executors and renderers with a conformance suite.

### Distribution
- Wheel for `uv tool install` / `pipx`, single-file binaries for Linux, macOS and Windows.

### Known limits
- Live provider contract tests and live evals have not been run for this release (they need
  API keys); see `PROGRESS.md`, *Open issues*.
- No OS sandbox on Windows yet; see `docs/SECURITY.md`.
