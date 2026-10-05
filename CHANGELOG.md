# Changelog

All notable changes to Forge. Versions follow [Semantic Versioning](https://semver.org).

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
