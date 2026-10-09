# Forge — Step-by-step build plan

Forge v1 is built in 48 step cards across six phases, written for a coding agent following `AGENTS.md`: one card per session, in order. Every card lists the files to touch, what to build, the tests to write first, and one verify command that must pass. Contracts named on a card are in `docs/CONTRACTS.md` and are binding.

The gate command from `AGENTS.md` (ruff, mypy, pytest) must also pass after every card. Tick a card only when both pass.

## Phase 0 · Foundation (weeks 1–2)

- [ ] **S01 — Repo skeleton**
    - Files: `pyproject.toml`, `src/forge/__init__.py`, `src/forge/__main__.py`, `src/forge/cli.py`, `tests/test_smoke.py`, `.github/workflows/ci.yml`, `PROGRESS.md`, `.gitignore`
    - Build: package `forge` (src layout) with console script `forge`; `forge --version`; ruff and `mypy --strict` config in `pyproject.toml`; CI matrix ubuntu, macos, windows running the gate command; `PROGRESS.md` in the AGENTS.md format.
    - Tests: importing `forge` works; `forge --version` prints `forge 0.1.0`.
    - Verify: `uv run forge --version`
- [ ] **S02 — Config**
    - Contracts: Config schema
    - Files: `src/forge/config.py`, `forge.example.toml`, `tests/test_config.py`, `cli.py`
    - Build: `ForgeConfig`; `load_config(project_root, profile=None, overrides={})` with the layering and trust rules; `forge config check` prints the effective config as TOML with secrets masked.
    - Tests: later layer wins; `FORGE_SANDBOX__MODE` env override; untrusted project cannot set `providers`; `forge.example.toml` loads without errors; unknown key gives a clear error.
    - Verify: `uv run pytest tests/test_config.py -q && uv run forge config check`
- [ ] **S03 — Messages and events**
    - Contracts: Messages and events
    - Files: `src/forge/providers/__init__.py`, `src/forge/providers/base.py` (message types only), `src/forge/events.py`, `tests/test_messages.py`
    - Build: every model from the contract.
    - Tests: JSON round trip for each type; every `Event` serializes to exactly one line; a tool-call message survives a round trip unchanged.
    - Verify: `uv run pytest tests/test_messages.py -q`
- [ ] **S04 — Ports and Ctx**
    - Contracts: Ports, Tool framework (`Ctx` only)
    - Files: `src/forge/ports.py`, `src/forge/ctx.py`, `src/forge/local/memory_bus.py`, `src/forge/local/memory_store.py`, `tests/test_ports.py`, `tests/test_architecture.py`
    - Build: the four protocols and their data models; `MemoryBus` (asyncio queue per subscriber); `MemoryStore` (dict-backed, used until S19).
    - Tests: publish/subscribe order; store save/load round trip; `test_architecture.py` parses imports with `ast` and fails if core modules import `textual`, `rich`, `sqlite3`, `sqlalchemy`, `subprocess` or `forge.local`.
    - Verify: `uv run pytest tests/test_ports.py tests/test_architecture.py -q`
- [ ] **S05 — First provider**
    - Contracts: Provider interface
    - Files: `src/forge/providers/base.py` (rest of contract), `src/forge/providers/openai_compat.py`, `src/forge/providers/registry.py`, `src/forge/providers/fake.py`, `tests/test_openai_compat.py`, `tests/live/test_live_openai.py`
    - Build: OpenAI chat-completions streaming with native tool calls, retries per contract, `ProviderError`, usage; `get_provider`; `FakeProvider` replaying scripted turns.
    - Tests (offline, `respx`): text streaming, a tool call split across chunks, 429 then success, 401 raises `auth`. Live (marked): one call to OpenAI and one to a local Ollama if reachable.
    - Verify: `uv run pytest tests/test_openai_compat.py -q`
- [ ] **S06 — Tool framework**
    - Contracts: Tool framework
    - Files: `src/forge/tools.py` (framework part only), `src/forge/runtime/permissions.py` (stub: `auto` → run, `ask` → ask), `src/forge/hooks.py` (stub: no hooks), `tests/test_tool_framework.py`, `tests/fixtures/schemas/`
    - Build: `@tool`, `REGISTRY`, schema generation from signature + `Annotated` + docstring, `call_tool` pipeline, output capping and spill to `.forge/out/`.
    - Tests: schema of a dummy tool equals a golden JSON file; invalid args return `ok=False` naming the field; 50,000-char output is spilled with a 2,000-char preview; `ask` tools call `renderer.approve`.
    - Verify: `uv run pytest tests/test_tool_framework.py -q`
- [ ] **S07 — Core file tools**
    - Contracts: Tool framework; see `docs/TOOLS.md` Files and Search
    - Files: `src/forge/tools.py`, `src/forge/runtime/files.py`, `src/forge/runtime/ledger.py`, `src/forge/runtime/ignore.py`, `tests/test_tools_files.py`
    - Build: `read_file`, `write_file`, `edit_file`, `list_dir`, `grep` (rg or Python fallback), `glob`; read ledger; shared ignore list.
    - Tests: read with offset/limit and `PARTIAL` note; write to unread existing file fails; edit not found / not unique / `replace_all`; CRLF preserved; grep respects `.gitignore`; glob newest first, cap 100.
    - Verify: `uv run pytest tests/test_tools_files.py -q`
- [ ] **S08 — Shell runtime**
    - Files: `src/forge/runtime/shell.py`, `src/forge/local/local_executor.py`, `src/forge/tools.py`, `tests/test_shell.py`
    - Build: persistent bash and PowerShell sessions with sentinel-based capture; `LocalExecutor` (sandbox comes in S28: record the policy, do not enforce yet); background jobs; tools `bash`, `powershell`, `job_output`, `job_stop`; benign exit codes; `CI=1`, `PAGER=cat`, stdin closed.
    - Tests: `cd` persists; leaving the root resets cwd; timeout moves to background; `grep` exit 1 is ok; stop a job; PowerShell tests skip when `pwsh` is missing.
    - Verify: `uv run pytest tests/test_shell.py -q` (CI runs it on all three OSes)
- [ ] **S09 — Agent loop**
    - Contracts: Agent loop
    - Files: `src/forge/agent.py`, `tests/test_agent.py`
    - Build: `run_agent` without compaction (a no-op hook point for S27); `max_turns`; publishes `ModelDelta`, `ToolStarted`, `ToolFinished`, `ModelDone`.
    - Tests (FakeProvider): reads then edits a file and stops; stops at `max_turns` with `stopped="max_turns"`; a failing tool result reaches the next model turn; parallel only for read-only calls.
    - Verify: `uv run pytest tests/test_agent.py -q`
- [ ] **S10 — Plain CLI and Phase 0 gate**
    - Files: `src/forge/cli.py`, `src/forge/local/rich_renderer.py`, `src/forge/prompts.py` (`BASE` and `CODER` only), `examples/buggy/` (tiny project with one failing test), `tests/e2e/test_fix_bug.py`
    - Build: `forge "<prompt>"` runs `run_agent` directly (pipeline comes in Phase 1); Rich renderer streams text, shows tool calls, asks y/n via `approve`. Global flag --fake <script.json> swaps every role to FakeProvider with a scripted fixture (default: tests/fixtures/fake/hello.json), so every later verify command can run offline.
    - Tests: e2e with FakeProvider fixes `examples/buggy`; live variant marked.
    - Verify: `uv run pytest tests/e2e -q` — Phase 0 gate: Forge fixes a seeded bug end to end.

## Phase 1 · Pipeline (weeks 3–5)

- [ ] **S11 — prompts.py**
    - Files: `src/forge/prompts.py`, `tests/test_prompts.py`
    - Build: `BASE`, `TOOL_RULES`, `REFINER`, `PLANNER`, `REPLANNER`, `CODER`, `STEP`, `REVIEWER`, `COMPRESSOR`, `TOOL_FALLBACK` (team prompts in S30); table of contents at the top; `render(name, **ctx)`; `OVERRIDES` per model family; `PROMPTS_VERSION`. Stable text first, volatile slots last.
    - Tests: every prompt renders with a full context; a missing slot raises; an unknown slot raises; `test_architecture.py` gains a check that no other module contains a string over 200 chars that looks like an instruction (heuristic: starts with "You are").
    - Verify: `uv run pytest tests/test_prompts.py tests/test_architecture.py -q`
- [ ] **S12 — Memory files**
    - Files: `src/forge/memory.py`, `tests/test_memory.py`
    - Build: load `~/.forge/FORGE.md`, then `FORGE.md`, `AGENTS.md`, `CLAUDE.md` from repo root down to cwd; each tagged with its folder scope; size cap 32 KB per file with a truncation note.
    - Tests: nested fixture resolves files in the right order; deeper file listed later; missing files ignored.
    - Verify: `uv run pytest tests/test_memory.py -q`
- [ ] **S13 — Plan models**
    - Contracts: Plan models
    - Files: `src/forge/plan.py`, `tests/test_plan.py`
    - Build: all models and the three methods.
    - Tests: `next_ready_step` respects dependencies and skipped steps; `validate_graph` finds duplicates, unknown deps, cycles, missing checks; `ready_steps` returns independent steps.
    - Verify: `uv run pytest tests/test_plan.py -q`
- [ ] **S14 — Refine stage**
    - Contracts: Plan models (`refine`)
    - Files: `src/forge/pipeline.py`, `src/forge/context.py`, `tests/test_refine.py`, `tests/fixtures/refine/`
    - Build: `context.gather(ctx)` (file tree to depth 3, git status, memory files, last session summary); `refine()` calls the `refiner` role with `json_schema` = `TaskSpec` schema, falls back to parsing a JSON block, retries once on invalid JSON.
    - Tests: 10 recorded prompts produce valid `TaskSpec`s; invalid JSON then valid JSON succeeds; a typo-fix prompt yields `size="trivial"`.
    - Verify: `uv run pytest tests/test_refine.py -q`
- [ ] **S15 — ask_user and clarify**
    - Files: `src/forge/tools.py` (`ask_user`), `src/forge/pipeline.py` (`clarify`), `src/forge/local/rich_renderer.py` (`ask`), `tests/test_clarify.py`
    - Build: `ask_user` per `docs/TOOLS.md`; `clarify()` loops max `limits.max_clarify_rounds`, merges answers into the spec, stops on `/go`; headless uses defaults and appends them to `assumptions`.
    - Tests: ambiguous fixture asks, answers end up in the spec; headless records assumptions; round limit respected; no question is asked about a fact present in the repo (fixture with the answer in README).
    - Verify: `uv run pytest tests/test_clarify.py -q`
- [ ] **S16 — Plan stage**
    - Files: `src/forge/tools.py` (`submit_plan`), `src/forge/pipeline.py` (`make_plan`), `tests/test_make_plan.py`
    - Build: planner role runs with read-only tools + `submit_plan`; validation via `validate_graph`, errors returned to the planner; user approve / edit / reject (headless: auto-approve).
    - Tests: a spec turns into a valid 3–12 step plan; an invalid plan is rejected with errors and fixed on retry; reject ends the session cleanly.
    - Verify: `uv run pytest tests/test_make_plan.py -q`
- [ ] **S17 — Execute and verify**
    - Files: `src/forge/tools.py` (`update_plan`, `finish_step`), `src/forge/pipeline.py` (`execute`, `verify_step`, `replan`), `tests/test_execute.py`
    - Build: one step at a time via `run_agent` with the `STEP` prompt; `finish_step` triggers `verify_step` (shell check or `review:` check); up to `max_step_attempts` retries with the failure; then `replan` revises remaining steps and bumps `version`.
    - Tests: all steps pass; step 2 fails once then passes; step 2 fails 3 times and is replanned; the model cannot set `done` without a passing check.
    - Verify: `uv run pytest tests/test_execute.py -q`
- [ ] **S18 — Checkpoints and /undo**
    - Files: `src/forge/runtime/checkpoint.py`, `src/forge/commands.py` (`/undo`), `tests/test_checkpoint.py`
    - Build: before each step, snapshot the working tree to a hidden ref `refs/forge/<session>/<step>` (git plumbing: `write-tree` on a temp index); `/undo` restores the previous snapshot without touching the user's branch or staged changes.
    - Tests: undo after step 3 equals the step-2 tree; untracked files are covered; the user's HEAD and index are unchanged.
    - Verify: `uv run pytest tests/test_checkpoint.py -q`
- [ ] **S19 — SQLite store and resume**
    - Contracts: Ports (`Store`)
    - Files: `src/forge/local/sqlite_store.py`, `src/forge/cli.py` (`forge resume`, `forge sessions`), `tests/test_sqlite_store.py`, `tests/e2e/test_resume.py`
    - Build: `SqliteStore` at `~/.forge/forge.db` (SQLAlchemy async + aiosqlite), FTS5 index for `search`; pipeline saves after every step; `forge resume [id]` continues at the first unfinished step.
    - Tests: same tests as `MemoryStore` pass; kill mid-plan (cancel the task) then resume completes the plan.
    - Verify: `uv run pytest tests/test_sqlite_store.py tests/e2e/test_resume.py -q` — Phase 1 gate.
- [ ] **S20 — Final review and starter evals**
    - Files: `src/forge/pipeline.py` (`final_review`, `run_task`), `src/forge/evals.py`, `evals/tasks/*.toml` (10 tasks), `src/forge/cli.py` (`forge eval`), `tests/test_review.py`
    - Build: reviewer checks the full diff against acceptance criteria and returns a `Report`; `forge "<prompt>"` now runs the whole pipeline; `forge eval` runs tasks (repo + prompt + check command) and prints pass/fail, cost, time.
    - Tests: the report lists every assumption from clarify; `forge eval --fake` runs offline with fixtures.
    - Verify: `uv run pytest tests/test_review.py -q && uv run forge eval --fake`

## Phase 2 · Breadth (weeks 6–8)

- [ ] **S21 — Provider registry**
    - Contracts: Provider interface (`registry.py`), Config schema
    - Files: `src/forge/providers/registry.py`, `src/forge/providers/catalog.py`, `tests/test_registry.py`
    - Build: build providers from `[providers.*]`; model catalog with `Capabilities` for common models plus `[models.*]` overrides; `resolve_role` fallback chains; on `ProviderError` the agent loop moves to the next model.
    - Tests: changing a role in config changes the model used; unknown model gets safe defaults; fallback after `overloaded` reaches the second model.
    - Verify: `uv run pytest tests/test_registry.py -q`
- [ ] **S22 — Anthropic adapter**
    - Files: `src/forge/providers/anthropic.py`, `tests/test_anthropic.py`, `tests/contract/test_provider_contract.py`
    - Build: streaming, tool use, cache-control marker on the stable system prefix, extended thinking (keep `reasoning` for round trips), Bedrock and Vertex via the SDK's clients. Write the shared provider contract test now; it runs against every adapter.
    - Tests: offline with recorded fixtures; contract test (live, marked) passes; usage shows `cached_tokens` on the second call.
    - Verify: `uv run pytest tests/test_anthropic.py tests/contract -q`
- [ ] **S23 — Google adapter**
    - Files: `src/forge/providers/google.py`, `tests/test_google.py`
    - Build: Gemini API and Vertex, function calling, usage.
    - Tests: recorded fixtures; contract test (live, marked).
    - Verify: `uv run pytest tests/test_google.py -q`
- [ ] **S24 — OpenAI Responses wire**
    - Files: `src/forge/providers/openai_compat.py`, `tests/test_openai_responses.py`
    - Build: `wire = "responses"` path next to `chat`, selected per provider in config.
    - Tests: the same offline cases as S05 pass for both wires.
    - Verify: `uv run pytest tests/test_openai_compat.py tests/test_openai_responses.py -q`
- [ ] **S25 — LiteLLM and tool fallback**
    - Files: `src/forge/providers/litellm.py`, `src/forge/providers/fallback_tools.py`, `tests/test_fallback_tools.py`, `docs/PROVIDERS.md`
    - Build: LiteLLM adapter; prompt-based tool calling (`TOOL_FALLBACK`) when `capabilities.tools` is false: schema in the prompt, parse fenced JSON into `ToolCall`s.
    - Tests: fallback parser handles one call, several calls, malformed JSON (returns an error turn); `docs/PROVIDERS.md` lists 10 tested providers with config snippets.
    - Verify: `uv run pytest tests/test_fallback_tools.py -q && uv run pytest tests/contract -m live` — Phase 2 gate: 10 providers pass.
- [ ] **S26 — Remaining tools**
    - Files: `src/forge/tools.py`, `src/forge/runtime/patch.py`, `src/forge/runtime/repomap.py`, `src/forge/runtime/web.py`, `tests/test_patch.py`, `tests/test_repomap.py`, `tests/test_web.py`, `tests/test_memory_tools.py`
    - Build: `apply_patch` (Codex envelope, atomic), `repo_map`, `web_fetch`, `web_search` (native or configured backend), `remember`, `recall` — all per `docs/TOOLS.md`.
    - Tests: patch add/update/delete/move, failing hunk changes nothing; repo map respects the token budget; fetch refuses localhost and reports cross-host redirects; `remember` appends to `FORGE.md`.
    - Verify: `uv run pytest tests/test_patch.py tests/test_repomap.py tests/test_web.py tests/test_memory_tools.py -q`
- [ ] **S27 — Compression**
    - Files: `src/forge/compress.py`, `src/forge/agent.py` (hook point), `src/forge/commands.py` (`/compact`, `/context`), `tests/test_compress.py`
    - Build: per-request token budget from `Capabilities`; level 1 trim every turn; level 2 summary at `compact_at` via the `compressor` role; level 3 reset at `reset_at` (system + spec + plan + summary + touched files); emit `Compacted`.
    - Tests: 200-turn fixture stays under an 8,000-token window; plan and unresolved errors survive every level; transcript in the store stays complete.
    - Verify: `uv run pytest tests/test_compress.py -q`
- [ ] **S28 — Permissions and sandbox**
    - Contracts: Tool framework (`Permissions`), Ports (`SandboxPolicy`)
    - Files: `src/forge/runtime/permissions.py`, `src/forge/runtime/rules.py`, `src/forge/runtime/sandbox.py`, `src/forge/local/local_executor.py`, `tests/test_permissions.py`, `tests/test_sandbox.py`
    - Build: rule parser for `tool(specifier)` with glob matching; evaluation order deny → ask → allow → read-only list → sandbox × approval; OS sandboxes: Seatbelt profile (macOS), bubblewrap or Landlock (Linux), restricted token + job object (Windows); `sandbox_denied` in results.
    - Tests: rule matrix (20+ cases); a `read_file` deny also blocks `edit_file`; `workspace-write` blocks a write outside the root on each OS; network off blocks `curl`.
    - Verify: `uv run pytest tests/test_permissions.py tests/test_sandbox.py -q`
- [ ] **S29 — Terminal UI**
    - Files: `src/forge/tui.py`, `src/forge/local/tui_renderer.py`, `src/forge/commands.py`, `tests/test_tui.py`
    - Build: Textual app: streaming pane, live plan checklist, question picker with "Other", diff view for edits, approval dialog, slash commands `/plan`, `/compact`, `/context`, `/undo`, `/mode`, `/jobs`, `/help`. `forge` with no prompt opens the TUI.
    - Tests: Textual pilot tests for asking, approving and plan updates with FakeProvider.
    - Verify: `uv run pytest tests/test_tui.py -q`

## Phase 3 · Teams (weeks 9–11)

- [ ] **S30 — Sub-agents**
    - Contracts: Agent loop, Tool framework (`for_role`)
    - Files: `src/forge/team.py`, `src/forge/tools.py` (`spawn_agent`), `src/forge/prompts.py` (`TEAM_LEAD`, `TEAM_MEMBER`, `EXPLORE`), `tests/test_subagents.py`
    - Build: foreground `spawn_agent`: new `Ctx` with its own `agent_id`, role tool subset, same or stricter sandbox, no `ask_user` or `spawn_agent`; returns only the final text; built-in roles `explore`, `coder`, `tester`, `reviewer`, `researcher`.
    - Tests: the parent's context grows only by the summary; a reviewer sub-agent cannot call `edit_file`; `max_turns` gives a partial result flag.
    - Verify: `uv run pytest tests/test_subagents.py -q`
- [ ] **S31 — Custom agent files**
    - Files: `src/forge/team.py` (loader), `tests/test_agent_files.py`, `tests/fixtures/agents/`
    - Build: load `.forge/agents/*.md` and `~/.forge/agents/*.md` (YAML-like front matter: `name`, `model`, `tools`, `description`; body = prompt); project overrides user on name clash.
    - Tests: a custom role runs with its own model and tools; invalid front matter gives a clear error naming the file.
    - Verify: `uv run pytest tests/test_agent_files.py -q`
- [ ] **S32 — Background agents and messages**
    - Files: `src/forge/team.py`, `src/forge/tools.py` (`send_message`, `list_agents`, `stop_agent`), `tests/test_messaging.py`
    - Build: `background=True` runs the agent as an asyncio task; per-agent inbox delivered at the next loop step; completion publishes an event and posts the summary to the parent's inbox.
    - Tests: two background agents exchange a message and both finish; `stop_agent` cancels and stops its jobs; `list_agents` shows status and tokens.
    - Verify: `uv run pytest tests/test_messaging.py -q`
- [ ] **S33 — Task board**
    - Files: `src/forge/team.py` (board), `src/forge/tools.py` (`read_board`, `claim_task`, `update_task`), `src/forge/local/sqlite_store.py` (board table), `tests/test_board.py`
    - Build: plan steps mirrored as board tasks in the store; atomic claim (transaction with status check); `update_task(done)` runs the same verify as `finish_step`.
    - Tests: 4 agents claim 8 independent tasks with no double claims (run 50 times); a task with unmet dependencies cannot be claimed.
    - Verify: `uv run pytest tests/test_board.py -q`
- [ ] **S34 — Worktrees and merge**
    - Files: `src/forge/runtime/worktree.py`, `src/forge/team.py` (merge flow), `tests/test_worktree.py`
    - Build: `isolation="worktree"` creates `.forge/worktrees/<agent>` on branch `forge/<session>/<agent>`; the lead merges finished tasks one by one, the reviewer checks each merge diff; a conflict goes back to the task owner; worktrees removed at session end unless kept.
    - Tests: parallel edits to different files merge cleanly; a conflict is reported to the owner, not auto-resolved.
    - Verify: `uv run pytest tests/test_worktree.py -q`
- [ ] **S35 — Budgets and mode selection**
    - Files: `src/forge/team.py`, `src/forge/pipeline.py`, `src/forge/cli.py` (`--solo`, `--team`), `tests/test_budgets.py`
    - Build: shared budget across all agents (tokens, cost, parallel count); `TaskSpec.size` picks solo (trivial, small), sub-agents (medium) or team (large); flags override.
    - Tests: the session stops cleanly at `max_cost_usd` with a report; size → mode mapping; flags win.
    - Verify: `uv run pytest tests/test_budgets.py -q`
- [ ] **S36 — MCP client and Phase 3 gate**
    - Files: `src/forge/mcp_client.py`, `src/forge/tools.py` (`list_mcp_resources`, `read_mcp_resource`, `tool_search`), `tests/test_mcp.py`, `evals/tasks/large_*.toml`
    - Build: connect `[mcp_servers.*]` over stdio and HTTP; register tools as `mcp__<server>__<tool>` through the normal `call_tool` pipeline; defer schemas above 40 tools and load them with `tool_search`; add 5 large multi-file eval tasks.
    - Tests: a stub MCP server's tool is callable and permission-checked; deferred loading works.
    - Verify: `uv run pytest tests/test_mcp.py -q && uv run forge eval --suite large --compare solo,team` — Phase 3 gate: team passes more large tasks than solo.

## Phase 4 · Extensibility (weeks 12–15)

- [ ] **S37 — Hooks**
    - Contracts: Tool framework (`HookEvent`, `HookOutcome`), Config schema (`[hooks]`)
    - Files: `src/forge/hooks.py`, `src/forge/runtime/hook_runner.py`, `tests/test_hooks.py`
    - Build: all 8 events wired into pipeline, agent loop, compaction and sub-agents; shell hooks get the event JSON on stdin (exit 0 continue, exit 2 block with stderr to the model, other = warning); Python hooks via `@hook("pre_tool")`; `match` regex on tool names; 30 s timeout per hook.
    - Tests: `post_tool` formatter runs after `edit_file`; `pre_tool` blocks `bash(git push*)` and the model sees the reason; a hanging hook times out without stopping the session.
    - Verify: `uv run pytest tests/test_hooks.py -q`
- [ ] **S38 — Skills**
    - Files: `src/forge/skills.py`, `src/forge/prompts.py` (skills list slot), `tests/test_skills.py`
    - Build: discover `.forge/skills/*/SKILL.md` and `~/.forge/skills/*/SKILL.md`; only name + description go into the prompt; the agent loads a full skill with `read_file` on its path (no new tool in v1).
    - Tests: skills list renders; a fixture task makes the FakeProvider read the matching skill.
    - Verify: `uv run pytest tests/test_skills.py -q`
- [ ] **S39 — Slash commands**
    - Files: `src/forge/commands.py`, `tests/test_commands.py`
    - Build: built-ins (`/plan`, `/go`, `/compact`, `/context`, `/undo`, `/mode`, `/jobs`, `/agents`, `/init`, `/help`); custom commands from `.forge/commands/*.md` with `$ARGUMENTS`; `/init` scaffolds a `FORGE.md` from the repo (build and test commands, layout).
    - Tests: `/review src/` expands a custom template; `/init` writes a sensible `FORGE.md` for `examples/buggy`.
    - Verify: `uv run pytest tests/test_commands.py -q`
- [ ] **S40 — Headless mode**
    - Files: `src/forge/cli.py` (`forge run`), `src/forge/local/json_renderer.py`, `tests/e2e/test_headless.py`
    - Build: `forge run --json --yes "<prompt>"` prints one event per line (contract events only); questions use defaults; approvals follow config (no prompts); exit codes 0 done, 1 failed, 2 needs input (when `--no-defaults`).
    - Tests: output parses line by line as events; exit codes for each case.
    - Verify: `uv run pytest tests/e2e/test_headless.py -q`
- [ ] **S41 — Python API**
    - Files: `src/forge/api.py`, `src/forge/__init__.py` (export `Forge`), `examples/embed.py`, `tests/test_api.py`
    - Build: `Forge(config=None, renderer=None, store=None, executor=None)`; `await forge.run(prompt) -> Report`; `async for event in forge.stream(prompt)`; injected ports replace the local ones.
    - Tests: a custom renderer answers questions programmatically; a custom store receives saves.
    - Verify: `uv run pytest tests/test_api.py -q && uv run python examples/embed.py --fake`
- [ ] **S42 — Profiles and trust**
    - Files: `src/forge/config.py`, `src/forge/cli.py` (`-p`, `forge trust`), `tests/test_trust.py`
    - Build: `[profiles.*]` merged over the base config; `forge trust` records the project path in `~/.forge/trusted.toml`; untrusted project values for providers, MCP servers and hooks are ignored with a visible warning.
    - Tests: `-p ci` applies; an untrusted `.forge/config.toml` cannot change `base_url` or add a hook.
    - Verify: `uv run pytest tests/test_trust.py -q`
- [ ] **S43 — Port conformance suite and Phase 4 gate**
    - Files: `tests/conformance/` (one parametrized suite per port), `src/forge/local/json_store.py` (toy second store), `examples/plugin_demo/` (a hook + an MCP tool)
    - Build: every `Store`, `EventBus`, `Executor`, `Renderer` implementation is run through the same suite; `JsonStore` proves a second store works with zero core changes.
    - Tests: `MemoryStore`, `SqliteStore`, `JsonStore` all pass; `plugin_demo` adds a hook and a tool without editing `src/forge/`.
    - Verify: `uv run pytest tests/conformance -q && git diff --stat HEAD~1 -- src/forge/pipeline.py src/forge/agent.py` (must be empty) — Phase 4 gate.

## Phase 5 · Hardening (weeks 16–18)

- [ ] **S44 — Eval suite**
    - Files: `evals/tasks/` (30–50 tasks), `src/forge/evals.py`, `evals/RESULTS.md`
    - Build: tasks across bug fix, feature, refactor, multi-file and Windows-specific; SWE-bench Lite runner; results table per model and `PROMPTS_VERSION` with pass rate, cost, time.
    - Tests: `forge eval --fake` stays offline and deterministic.
    - Verify: `uv run forge eval --fake && uv run forge eval --models 5 --report evals/RESULTS.md` (live)
- [ ] **S45 — Cross-OS CI**
    - Files: `.github/workflows/ci.yml`, OS-specific fixes
    - Build: matrix ubuntu / macos / windows × Python 3.12 / 3.13; Windows jobs with PowerShell 5.1, PowerShell 7 and Git Bash; sandbox tests per OS.
    - Verify: all CI jobs green on the main branch.
- [ ] **S46 — Security review**
    - Files: `tests/security/`, fixes where needed, `docs/SECURITY.md`
    - Build: tests for sandbox escape (symlinks, `..`, absolute paths), rule bypass (`powershell` when `bash` is denied, `sh -c` wrappers), secret masking in outputs, prompt injection in fetched pages and repo files (agent must not follow embedded instructions to change rules).
    - Verify: `uv run pytest tests/security -q`; every found issue has a regression test.
- [ ] **S47 — Documentation**
    - Files: `README.md`, `docs/quickstart.md`, `docs/config.md` (generated from `ForgeConfig`), `docs/extending.md`, `examples/`
    - Build: install, first task, providers, permissions, hooks, agents, skills, commands, headless, Python API; config reference generated by `forge config schema --markdown`.
    - Verify: `uv run forge config schema --markdown | diff - docs/config.md` (no diff); a fresh checkout follows the quick start successfully.
- [ ] **S48 — Release v1.0**
    - Files: `pyproject.toml` (version `1.0.0`), `CHANGELOG.md`, `.github/workflows/release.yml`, `packaging/pyinstaller.spec`
    - Build: `uv tool install forge` from the built wheel; PyInstaller single binaries per OS; release workflow on tag.
    - Verify: on each OS, a clean machine runs `forge --version` and `forge run --json --yes "say hi" --fake` successfully.

## Phase 6 · After v1.0: research, browser, streaming, background work

- [ ] **S49 — Research tool and web search fallback**
    - Files: `tools.py`, `prompts.py`, `agent.py`, `team.py`, `config.py`, `config_docs.py`, `docs/config.md`, `tests/test_research.py`
    - Build: `research(question, browser, depth)` starts a researcher sub-agent (also in solo mode, main agent only) and returns its report; RESEARCHER prompt; CODER/TEAM_LEAD prefer `research` for anything beyond a single fact; `web.fallback_backend` used when the model has no native search.
    - Verify: `uv run pytest tests/test_research.py -q`.
- [ ] **S50 — Browser agent with screenshots**
    - Files: `ports.py` (`Browser`, `BrowserFactory`), `ctx.py`, `local/playwright_browser.py`, `tools.py` (browser group), `prompts.py` (BROWSER), `team.py`, `config.py`, `cli.py` (`forge browser install`), `wiring.py`, `packaging/pyinstaller.spec`, `tests/test_browser.py`, `tests/conformance/test_browser_conformance.py`
    - Build: a `browser` role whose tools open, click, type, scroll, read and go back, each returning a screenshot; URLs pass `checked_url` and permission rules; `research(browser=true)` starts it, only when the model has vision.
    - Verify: `uv run pytest tests/test_browser.py tests/conformance -q` (Chromium integration skipped when no browser is installed).
- [ ] **S51 — Live tool output**
    - Files: `events.py` (`ToolOutput`), `ports.py` (`Executor.run(on_output=...)`), `local/local_executor.py`, `runtime/shell.py`, `tools.py`, renderers, conformance suites
    - Build: shell output reaches the renderers while the command runs, throttled; the final result is unchanged.
    - Verify: `uv run pytest tests/test_live_output.py tests/conformance -q`.
- [ ] **S52 — Early tool start**
    - Files: `providers/base.py` (`StreamItem.tool_call`), provider adapters, `modelcall.py`, `agent.py` or `early_tools.py`
    - Build: read-only tools that need no approval start as soon as their call is complete in the stream; others wait for the full reply; a failed stream cancels early calls.
    - Verify: `uv run pytest tests/test_early_tools.py -q`.
- [ ] **S53 — Monitor tool**
    - Files: `tools.py` (`monitor`, `monitor_stop`), `monitors.py`, `agent.py`, `team.py`, `prompts.py`, `tests/test_monitor.py`
    - Build: a background command whose new output lines (optionally filtered) arrive as messages to the agent; the agent waits while monitors run.
    - Verify: `uv run pytest tests/test_monitor.py -q`.
- [ ] **S54 — Agent todo list**
    - Files: `tools.py` (`todo_write`), `events.py` (`TodosUpdated`), `ctx.py`, renderers, `prompts.py`
    - Build: the agent keeps a visible checklist for multi-step work outside a pipeline plan.
    - Verify: `uv run pytest -q -k todo`.
- [ ] **S55 — Background tasks view**
    - Files: `tasks_view.py`, `commands.py` (`/tasks`), `tui.py`, `local/tui_renderer.py`, `tests/test_tasks_view.py`
    - Build: one list of running jobs, agents and monitors with output and stop, as `/tasks` and a TUI panel.
    - Verify: `uv run pytest tests/test_tasks_view.py tests/test_tui.py -q`.
- [ ] **S56 — MCP server management**
    - Files: `mcp_admin.py`, `cli.py` (`forge mcp ...`), `commands.py` (`/mcp`), `mcp_client.py`, `tests/test_mcp_admin.py`
    - Build: `forge mcp add/add-json/list/get/remove` (user or project scope), `/mcp` status, add, remove and reconnect without restart.
    - Verify: `uv run pytest tests/test_mcp_admin.py -q`; `forge mcp add stub -- python tests/fixtures/mcp_stub.py && forge mcp list`.

- [ ] **S57 — Local models with Ollama**
    - Files: `ollama_setup.py`, `ollama_cli.py`, `config_edit.py` (shared with `mcp_admin.py`), `cli.py`, docs, `tests/test_ollama_setup.py`, `tests/test_config_edit.py`
    - Build: `forge ollama setup` (hardware-based model choice, pull, `forge-<model>` with a fitting `num_ctx`, roles and model entry written) and `forge ollama status`; warning for Ollama models without a known context window.
    - Verify: `uv run pytest tests/test_ollama_setup.py -q`; live: `FORGE_LIVE_OLLAMA=1 uv run pytest -m live tests/contract -k ollama` and an end-to-end task on `examples/buggy`.

## Phase 7 · Apple apps (asked for after v1)

Forge builds native Apple apps (Swift, SwiftUI) for iPhone, iPad, Mac and Apple Watch. Xcode runs only on macOS, so builds go through a port: Forge's own implementation uses Xcode on the Mac Forge runs on; a server (Forge Web) implements the same port with a remote Mac. An independent reviewer checks the prompt, the plan and the product against Apple's guidelines, and the user approves the result before anything goes to Apple.

- [ ] **S58a — Apple builds as a port**
    - Contracts: Ports (`AppleBuilder`)
    - Files: `ports.py`, `ctx.py`, `config.py` (`[apple]`), `tools.py` (`apple` group), `runtime/apple.py`, `prompts.py` (APPLE), `agent.py`, `wiring.py`, `local/xcode_builder.py`, `tests/test_apple_tools.py`, `tests/test_xcode_builder.py`
    - Build: the `AppleBuilder` port (build, test and archive for iOS, iPadOS, macOS and watchOS; screenshots on simulated devices); tools `apple_build` and `apple_screenshot` (they ask first, like the shell, and are offered only when a builder is set); Apple guidance in the system prompt of Apple projects; `XcodeBuilder` runs XcodeGen, xcodebuild and simctl and is wired in on a Mac with Xcode.
    - Tests: the tools report errors, warnings, test counts and screenshots; without a builder there are no `apple` tools; the local builder's commands, scheme and device choice and its parsing, against fake `xcodebuild`, `xcrun` and `xcodegen`.
    - Verify: `uv run pytest tests/test_apple_tools.py tests/test_xcode_builder.py -q`
- [ ] **S58b — Apple app template**
    - Files: `apple_template.py`, `cli.py` (`forge apple new`), `.github/workflows/ci.yml` (macOS job with Xcode), `tests/test_apple_template.py`, `tests/apple/test_xcode_live.py`
    - Build: a SwiftUI app for iPhone/iPad, Mac and Apple Watch (XcodeGen `project.yml`, shared code, XCTest, a privacy manifest); `forge apple new <name>`; a macOS CI job builds and tests the template with Xcode and takes simulator screenshots through `XcodeBuilder` (marker `apple`, off by default).
    - Verify: `uv run pytest tests/test_apple_template.py -q`; on a Mac with Xcode: `uv run pytest -m apple -q`
- [ ] **S59 — Independent Apple guideline reviewer**
    - Contracts: Messages and events (`GuidelineReview`)
    - Files: `apple_review.py`, `prompts.py` (APPLE_REVIEWER), `config.py` (role `apple_reviewer`), `events.py`, renderers, `tests/test_apple_review.py`
    - Build: the role `apple_reviewer` works in a fresh context (none of the builder's reasoning) with read-only tools and `web_fetch` for Apple's current guidelines; `review_prompt`, `review_plan` and `review_product` return a verdict per guideline area (ok, concern, violation; guideline number, reason, fix); every review is a `GuidelineReview` event.
    - Verify: `uv run pytest tests/test_apple_review.py -q`
- [ ] **S60 — Apple checkpoints in the pipeline**
    - Files: `pipeline.py`, `apple_review.py`, `cli.py` (`--apple`), `config.py` (`apple.review`), `tests/test_apple_pipeline.py`
    - Build: with `--apple` (or `[apple] review = true`) the prompt is reviewed before planning, the plan after planning and the product after a green build with screenshots of every device; a violation stops the run until the agent revises it or the user overrides it; at the end the user approves the result before it counts as ready for Apple.
    - Verify: `uv run pytest tests/test_apple_pipeline.py -q`

**After v1.0 — server (not in scope now):** write `PostgresStore`, `RedisBus`, `DockerExecutor` and a `WebSocketRenderer` against the S43 suite, then add `src/forge/server/` (FastAPI + worker). No change to `pipeline.py`, `agent.py`, `tools.py` or `prompts.py` should be needed.
