# Progress

Next step: done (phase 6)

## Done
| Step | Date | Commit | Files | Notes |
| --- | --- | --- | --- | --- |
| S01 | 2026-10-05 | e06bc18 | pyproject.toml, src/forge/__init__.py, src/forge/__main__.py, src/forge/cli.py, tests/test_smoke.py, .github/workflows/ci.yml, PROGRESS.md, .gitignore, .python-version | `forge --version` prints `forge 0.1.0`; CI matrix ubuntu/macos/windows runs the gate |
| S02 | 2026-10-05 | 25b36b7 | src/forge/config.py, src/forge/toml_writer.py, forge.example.toml, tests/test_config.py, src/forge/cli.py | layering, env overrides, trust gate, profiles, `forge config check` with masked secrets |
| S03 | 2026-10-05 | 96a1af6 | src/forge/providers/__init__.py, src/forge/providers/base.py, src/forge/events.py, src/forge/plan.py, tests/test_messages.py | all contract message/event models; `parse_event` reads a JSON line back via a discriminated union on `kind` |
| S04 | 2026-10-05 | 6c6080c | src/forge/ports.py, src/forge/ctx.py, src/forge/local/__init__.py, src/forge/local/memory_bus.py, src/forge/local/memory_store.py, tests/test_ports.py, tests/test_architecture.py | four protocols + data models; MemoryBus (queue per subscriber, `*` = all sessions); MemoryStore with word search; ast-based import rules |
| S05 | 2026-10-05 | 2f7af39 | src/forge/providers/base.py, src/forge/providers/openai_compat.py, src/forge/providers/registry.py, src/forge/providers/fake.py, src/forge/providers/catalog.py, src/forge/providers/sse.py, src/forge/providers/retry.py, src/forge/providers/tokens.py, tests/test_openai_compat.py, tests/live/test_live_openai.py | chat-completions streaming with tool calls, retries 1/2/4/8/16 s, error mapping, usage + cost; FakeProvider with per-role queues; registry with cached instances |
| S06 | 2026-10-05 | 84d0caf | src/forge/tools.py, src/forge/runtime/__init__.py, src/forge/runtime/errors.py, src/forge/runtime/permissions.py, src/forge/hooks.py, src/forge/ctx.py, tests/test_tool_framework.py, tests/fixtures/schemas/dummy.json, tests/support.py, tests/conftest.py | @tool + make_tool_def, schema from signature/Annotated/docstring (refs inlined, titles dropped), call_tool pipeline with approval, hooks, spill to .forge/out/, audit log |
| S07 | 2026-10-05 | b040890 | src/forge/tools.py, src/forge/runtime/files.py, src/forge/runtime/ledger.py, src/forge/runtime/ignore.py, src/forge/runtime/search.py, src/forge/runtime/edit.py, src/forge/runtime/readers.py, src/forge/runtime/tree.py, src/forge/runtime/proc.py, src/forge/ctx.py, tests/test_tools_files.py, tests/support.py | read_file (text, images, PDF, notebooks), write_file, edit_file, list_dir, glob, grep (rg + identical Python fallback); read ledger; atomic writes with an undo journal |
| S08 | 2026-10-05 | c8cf016 | src/forge/runtime/shell.py, src/forge/local/local_executor.py, src/forge/tools.py, src/forge/ports.py, tests/test_shell.py | persistent bash/PowerShell with sentinel capture, pooled shells, background jobs with logs, timeout → background job, process-tree stop; tools bash, powershell, job_output, job_stop |
| S09 | 2026-10-05 | 03bc24e | src/forge/agent.py, src/forge/prompts.py, tests/test_agent.py, tests/support.py | run_agent: role prompt, fallback chain per turn, ModelDelta/ModelDone/ToolStarted/ToolFinished events, read-only calls in parallel, max_turns, cost stop, compaction hook point |
| S10 | 2026-10-05 | d0570c8 | src/forge/cli.py, src/forge/wiring.py, src/forge/local/rich_renderer.py, examples/buggy/, tests/e2e/test_fix_bug.py, tests/fixtures/fake/hello.json, tests/fixtures/fake/fix_buggy.json | Phase 0 gate passed: `forge --fake fix_buggy.json --yes` fixes examples/buggy end to end; Rich renderer streams text, tool calls, approvals |
| S11 | 2026-10-05 | 3af6d99 | src/forge/prompts.py, src/forge/agent.py, tests/test_prompts.py, tests/test_architecture.py | BASE, SAFETY, TOOL_RULES, REFINER, PLANNER, REPLANNER, CODER, STEP, REVIEWER, FINAL_REVIEW, COMPRESSOR, TOOL_FALLBACK; OVERRIDES per model family; render() checks slots |
| S12 | 2026-10-05 | cdacf36 | src/forge/memory.py, src/forge/agent.py, tests/test_memory.py | ~/.forge/FORGE.md, then FORGE.md/AGENTS.md/CLAUDE.md per folder root→cwd, 32 KB cap, rendered as <memory> blocks into the {memory} slot |
| S13 | 2026-10-05 | ca820c6 | src/forge/plan.py, tests/test_plan.py | next_ready_step, ready_steps, validate_graph (duplicates, unknown deps, self-deps, cycles, missing checks); checklist() |
| S14 | 2026-10-05 | f2b0084 | src/forge/pipeline.py, src/forge/context.py, src/forge/structured.py, src/forge/agent.py, src/forge/ctx.py, src/forge/prompts.py, tests/test_refine.py, tests/fixtures/refine/cases.json | context.gather (tree depth 3, git status, memory, last summary); refine() with json_schema, JSON-block fallback, one retry via FIX_JSON prompt |
| S15 | 2026-10-05 | 679b07f | src/forge/tools.py, src/forge/questions.py, src/forge/pipeline.py, src/forge/prompts.py, tests/test_clarify.py | ask_user tool (main agent only, validation per kind, headless defaults); clarify(): ask, merge answers via MERGE_ANSWERS, max rounds, /go |
| S16 | 2026-10-05 | 1ffa07f | src/forge/tools.py, src/forge/pipeline.py, src/forge/agent.py, src/forge/ctx.py, src/forge/prompts.py, tests/test_make_plan.py | submit_plan (graph + role + path validation, approval, versioning, replan keeps done steps); make_plan runs the planner role with read-only tools |
| S17 | 2026-10-05 | cdfc9a0 | src/forge/tools.py, src/forge/pipeline.py, src/forge/checks.py, src/forge/modelcall.py, src/forge/agent.py, src/forge/runtime/gitops.py, src/forge/plan.py, src/forge/prompts.py, src/forge/ctx.py, tests/test_execute.py | update_plan (transitions, done refused), finish_step → verify → done/check_failed/limit_reached; execute runs steps via run_agent with the STEP prompt, settles steps the agent did not finish, replans after max attempts |
| S18 | 2026-10-05 | 5746aea | src/forge/runtime/checkpoint.py, src/forge/commands.py, src/forge/pipeline.py, tests/test_checkpoint.py | snapshots via temp index + write-tree + commit-tree into refs/forge/<session>/<step>; restore via checkout-index from a temp index; /undo rolls back the latest step, else the last file change |
| S19 | 2026-10-05 | a37a6b3 | src/forge/local/sqlite_store.py, src/forge/pipeline.py, src/forge/cli.py, src/forge/wiring.py, src/forge/local/memory_store.py, src/forge/local/local_executor.py, tests/test_sqlite_store.py, tests/e2e/test_resume.py, pyproject.toml | Phase 1 gate passed: a plan killed mid-step resumes from the database and completes; SqliteStore (session JSON + FTS5 index + board table), forge sessions, forge resume [id] |
| S20 | 2026-10-05 | 5dab705 | src/forge/pipeline.py, src/forge/evals.py, src/forge/local/auto_renderer.py, src/forge/runtime/gitops.py, src/forge/wiring.py, src/forge/cli.py, evals/tasks/*.toml (10), evals/repos/, tests/test_review.py, tests/fixtures/fake/*.json | final_review → Report; run_task runs the whole pipeline (trivial tasks skip clarify/plan); forge eval with 10 tasks on 3 sample repos, offline via compact fake solutions |
| S21 | 2026-10-05 | 70e0862 | src/forge/providers/catalog.py, src/forge/providers/registry.py, tests/test_registry.py | 17 provider presets (zero-config for known vendors), model catalog with capabilities and prices, aliases (claude-sonnet → claude-sonnet-5-5), config overrides, fallback chain tested end to end |
| S22 | 2026-10-05 | 882de38 | providers/anthropic.py, providers/errors.py, providers/registry.py, tests/test_anthropic.py, tests/contract/test_provider_contract.py, tests/fixtures/anthropic/*.json | SDK client; offline via injected httpx2 MockTransport; contract test live-only |
| S23 | 2026-10-05 | a1f6b6f | providers/google.py, providers/registry.py, tests/test_google.py, tests/fixtures/google/*.json, tests/contract/test_provider_contract.py | google-genai SDK; Gemini API + Vertex; raw parts kept for thought signatures |
| S24 | 2026-10-05 | 2428362 | providers/openai_compat.py, providers/responses.py, tests/test_openai_responses.py | wire picks /chat/completions or /responses per provider |
| S25 | 2026-10-05 | 0665ab9 | providers/litellm.py, providers/fallback_tools.py, providers/registry.py, modelcall.py, docs/PROVIDERS.md, tests/test_fallback_tools.py, tests/contract/test_provider_contract.py | LiteLLM adapter; JSON tool-call fallback with one FIX_JSON error turn; PROVIDERS.md with 12 providers |
| S26a | 2026-10-05 | fc87adc | tools.py, runtime/patch.py, tests/test_patch.py, tests/test_memory_tools.py | apply_patch (atomic, 3-level matching), remember, recall; S26 split: S26b = repo_map, web_fetch, web_search |
| S26b | 2026-10-05 | e86c777 | tools.py, prompts.py, ctx.py, runtime/repomap.py, runtime/web.py, providers/anthropic.py, tests/test_repomap.py, tests/test_web.py, tests/test_anthropic.py | repo_map (tree-sitter tags + constants, ranked, cached), web_fetch (local targets refused, same-host redirects, 15 min cache), web_search (native Claude, brave, tavily, searxng) |
| S27 | 2026-10-05 | 56b3c42 | compress.py, agent.py, commands.py, ctx.py, prompts.py, tools.py, tests/test_compress.py | trim / summarize / reset; plan, spec, unresolved errors and touched files are copied by code; /compact [hard], /context |
| S28 | 2026-10-05 | 3e4f00c | runtime/permissions.py, runtime/rules.py, runtime/sandbox.py, runtime/shell.py, runtime/proc.py, local/local_executor.py, tools.py, tests/test_permissions.py, tests/test_sandbox.py | rules deny->ask->allow->read-only list->sandbox x approval; Landlock/bwrap (Linux), Seatbelt (macOS); blocked commands can be rerun outside the sandbox after approval |
| S29 | 2026-10-05 | dd9bf28 | tui.py, local/tui_renderer.py, commands.py, cli.py, tests/test_tui.py | Textual app: stream pane, live plan, question picker with Other, approval dialog with diff preview; /plan /compact /context /undo /mode /jobs /help; 'forge' alone opens it |
| S30 | 2026-10-05 | 45dde81 | team.py, tools.py, agent.py, ctx.py, prompts.py, wiring.py, tests/support.py, tests/test_subagents.py | foreground spawn_agent; child Ctx with own id/role/ledger; lead-only tools removed and enforced in call_tool; TEAM_LEAD/TEAM_MEMBER/EXPLORE prompts |
| S31 | 2026-10-05 | a323462 | team.py, ctx.py, agent.py, tools.py, prompts.py, tests/test_agent_files.py, tests/fixtures/agents/ | agent files (.forge/agents, ~/.forge/agents; project wins); custom role gets its model chain, tool list and prompt |
| S32 | 2026-10-05 | 031fbbd | team.py, agent_files.py, agent.py, ctx.py, events.py, tools.py, tests/test_messaging.py, tests/test_agent_files.py | background agents (asyncio tasks), inboxes drained each turn, lead waits for running children, send_message/list_agents/stop_agent |
| S33 | 2026-10-05 | 46ca63e | board.py, team.py, tools.py, ctx.py, ports.py, local/sqlite_store.py, local/memory_store.py, tests/test_board.py, tests/support.py | read_board/claim_task/update_task; atomic claims via BoardStore (SQLite conditional upsert); 50-round race on both stores |
| S34 | 2026-10-05 | 63f9012 | runtime/worktree.py, team.py, checks.py, ctx.py, wiring.py, tests/test_worktree.py | isolation=worktree; 3-way merge via git merge-tree into the live tree (user's index untouched); board tasks reviewed before merge; conflicts go back to the owner |
| S35 | 2026-10-05 | ac9623a | pipeline.py, team.py, agent.py, ctx.py, tools.py, prompts.py, cli.py, tests/test_budgets.py | size -> solo/subagents/team (--solo/--team win); shared cost budget checked before every turn; budget stop report; team mode runs board workers |
| S36 | 2026-10-05 | e046a9b | mcp_client.py, tools.py, agent.py, ctx.py, prompts.py, wiring.py, runtime/rules.py, evals.py, cli.py, tests/test_mcp.py, tests/fixtures/mcp_stub.py, evals/repos/shop/, evals/tasks/large_*.toml, tests/test_review.py | MCP over stdio/HTTP (mcp 2.x), mcp__<server>__<tool> tools per session, resources, deferred loading via tool_search; 5 large eval tasks; forge eval --compare |
| S37 | 2026-10-05 | 49427e7 | hooks.py, runtime/hook_runner.py, wiring.py, pipeline.py, tests/test_hooks.py | shell hooks (JSON on stdin, {placeholders}, exit 2 blocks, 30s timeout) + @hook Python hooks; all 8 events wired |
| S38 | 2026-10-05 | 6ebf5f6 | skills.py, prompts.py, agent.py, tests/test_skills.py, tests/fixtures/skills/ | SKILL.md discovery (project wins over user); name + description in the prompt via SKILLS; agents read skills with read_file |
| S39 | 2026-10-05 | 4b986d1 | commands.py, tui.py, tests/test_commands.py | built-ins incl. /go /agents /init; custom .forge/commands/*.md with $ARGUMENTS; /init detects pytest/npm/cargo/go/make commands and the layout |
| S40 | 2026-10-05 | 9405b08 | cli.py, local/json_renderer.py, tests/e2e/test_headless.py | forge run --json/--yes/--no-defaults: contract events as JSON lines, SessionDone last, exit 0/1/2 |
| S41 | 2026-10-05 | 37ee4e4 | api.py, __init__.py, runtime/gitops.py, examples/embed.py, tests/test_api.py | Forge(config, renderer, store, executor, root=, approve=); run() -> Report; stream() -> events ending with SessionDone; examples/embed.py --fake fixes examples/buggy in a temp copy |
| S42 | 2026-10-05 | 203200f | config.py, cli.py, tests/test_trust.py | forge trust [--remove] writes ~/.forge/trusted.toml; every command prints config warnings; profiles via -p |
| S43 | 2026-10-05 | 29bf75e | local/json_store.py, local/memory_store.py, tests/conformance/, examples/plugin_demo/ | conformance suites for Store/BoardStore (Memory, Sqlite, Json), EventBus, Executor, Renderer; JsonStore needs no core change; plugin_demo adds a hook + MCP tool via config only. Phase 4 gate: pipeline.py and agent.py unchanged in this commit. |
| S44 | 2026-10-05 | 3452965 | evals/tasks/ (33 tasks), evals/repos/logparse, evals/repos/winpaths, evals.py, eval_cli.py, swebench.py, cli.py, prompts.py, evals/RESULTS.md, tests/test_evals.py, tests/test_review.py, .gitignore | 33 offline-verified tasks (bug 7, feature 9, refactor 4, multi-file 3, windows 4, large 5, trivial 1); --models/--report results table with PROMPTS_VERSION; SWE-bench Lite runner (--swebench JSONL) |
| S45 | 2026-10-05 | c4078e4 | .github/workflows/ci.yml, runtime/shell.py, config.py, tests/test_shell.py | CI: ubuntu/macos/windows x Python 3.12/3.13 (+ offline evals), plus Windows jobs pinned to PowerShell 7, Windows PowerShell 5.1 and Git Bash via FORGE_POWERSHELL/FORGE_BASH |
| S46 | 2026-10-05 | da06385 | runtime/rules.py, runtime/permissions.py, runtime/secrets.py, tools.py, tests/security/, tests/test_permissions.py, docs/SECURITY.md | found and fixed: chained/wrapped/substituted commands bypassed deny rules; allow rules covered extra chained commands; bash rules did not cover powershell; reads outside the project ran without approval; no secret masking; edit_file revealed existence before the outside_root check |
| S47 | 2026-10-05 | 5f915e7 | config_docs.py, cli.py, docs/config.md, README.md, docs/quickstart.md, docs/extending.md, tests/test_docs.py, pyproject.toml | config reference generated from ForgeConfig; test keeps it equal |
| S48 | 2026-10-05 | a74d9a4 | pyproject.toml, __init__.py, CHANGELOG.md, .github/workflows/release.yml, packaging/pyinstaller.spec, README.md, tests/test_release.py, tests/test_smoke.py | wheel + PyInstaller binary smoke-tested locally (Linux); release workflow on tag v*; manual run green on Linux, macOS, Windows (wheel + binary smoke tests) |
| S49 | 2026-10-06 | e3f71db | tools.py, prompts.py, agent.py, config.py, config_docs.py, docs/config.md, docs/TOOLS.md, docs/STEPS.md, tests/test_research.py | research tool (researcher sub-agent, also in solo mode, lead only); RESEARCHER prompt; web.fallback_backend |
| S50 | 2026-10-06 | 990eb6b | ports.py, ctx.py, config.py, config_docs.py, tools.py, prompts.py, agent.py, team.py, wiring.py, cli.py, local/playwright_browser.py, pyproject.toml, packaging/pyinstaller.spec, .github/workflows/ci.yml, AGENTS.md, docs/*, README.md, tests/test_browser.py, tests/conformance/test_browser_conformance.py | browser role with 7 browser_* tools (screenshot per action), Browser port + Playwright implementation, research(browser=true), forge browser install; real-Chromium conformance passes locally |
| S51 | 2026-10-06 | 6094fc1 | events.py, ports.py, local/local_executor.py, runtime/shell.py, tools.py, local/json_renderer.py, local/rich_renderer.py, docs/CONTRACTS.md, tests/test_live_output.py, tests/test_messages.py, tests/support.py, tests/test_messaging.py, tests/conformance/test_executor_conformance.py, tests/fixtures/fake/live_output.json | ToolOutput events while bash/powershell run (whole lines, 0.2 s throttle, masked, max 500 lines per call); TUI and plain CLI show them dimmed |
| S52 | 2026-10-06 | 3b7c24d | providers/base.py, providers/anthropic.py, providers/openai_compat.py, providers/litellm.py, providers/responses.py, providers/google.py, providers/fake.py, modelcall.py, agent.py, tools.py, docs/CONTRACTS.md, tests/test_early_tools.py, tests/test_anthropic.py, tests/test_openai_compat.py, tests/test_openai_responses.py | StreamItem.tool_call from every adapter; EarlyTools starts safe reads during the stream; reply is authoritative (mismatch or failed stream cancels) |
| S53 | 2026-10-06 | 96b3ab9 | monitors.py, tools.py, agent.py, team.py, ctx.py, wiring.py, runtime/rules.py, prompts.py, docs/TOOLS.md, tests/test_monitor.py | monitor/monitor_stop: background command, new (filtered) lines as inbox messages, agent waits while monitors run; timeout and stop end the job |
| S54 | 2026-10-06 | dc9ac50 | todos.py, events.py, ctx.py, tools.py, prompts.py, tui.py, local/tui_renderer.py, local/rich_renderer.py, local/json_renderer.py, docs/TOOLS.md, docs/CONTRACTS.md, tests/test_todos.py, tests/test_messages.py | todo_write per agent, TodosUpdated event; TUI side panel under the plan, plain/JSON output; prompt rule for 3+ step work |
| S55 | 2026-10-06 | f9d72a3 | tasks_view.py, local/tasks_screen.py, tui.py, commands.py, ctx.py, tools.py, docs/quickstart.md, tests/test_tasks_view.py | /tasks [ID|stop ID] over jobs, agents and monitors; TUI bar 'N background tasks running' and ctrl+t list with output and stop |
| S56 | 2026-10-06 | b473c6d | mcp_admin.py, mcp_cli.py, mcp_client.py, cli.py, commands.py, docs/extending.md, docs/quickstart.md, README.md, tests/test_mcp_admin.py | forge mcp add/add-json/list/get/remove (user or project scope, connection check); /mcp status, add, remove, reconnect without restart |

## Decisions
- Session: the user asked for all steps to be built in one go, without stopping between steps, directly on `main`. This overrides "one step per session" (user instruction > AGENTS.md); every step still gets its own tests, gate run, PROGRESS.md entry and commit.
- S01: the Commit column is filled in by the following step's commit (a commit cannot contain its own hash).
- S01: build backend is hatchling; `.python-version` pins 3.12 for local work, matching CI.
- S01: ruff excludes `*.md`, because ruff 0.16 also formats Python blocks inside Markdown and would rewrite the binding contracts in `docs/`.
- S01: CI uses `concurrency` with `cancel-in-progress` so a quick series of pushes runs CI only once.
- S02: `src/forge/toml_writer.py` (extra file) serializes the effective config, because the standard library only reads TOML.
- S02: `FORGE_HOME` (default `~/.forge`) is reserved and not read as a config override; tests use it to isolate the user folder.
- S02: layer order is defaults → user file → project file → profile overlay → `FORGE_*` env → CLI overrides; the profile name itself may come from any layer. Lists replace, tables merge key by key.
- S02: an untrusted project's `providers`, `mcp_servers` and `hooks` are dropped (also inside its profiles) with a warning.
- S02: every layer is validated on its own first, so an unknown key error names the file it came from.
- S03: `plan.py` data models (Question, TaskSpec, Step, Plan; no methods) were created now because events reference `Question` and `Plan`; S13 adds the methods.
- S03: helpers beyond the contract: `Message.text()`, `Usage.__add__`, `text_message()`, `events.EVENT_TYPES` and `events.parse_event()`; no contract field changed.
- S04: `Ctx.ledger`, `Ctx.permissions` and `Ctx.hooks` are typed `Any` until their modules exist (S06, S07); the field names match the contract.
- S04: `MemoryBus.subscribe()` registers immediately (not lazily on first iteration) so no event published after the call is lost; a session subscription ends after that session's `SessionDone`.
- S04: `ports.SessionNotFoundError` (a `LookupError`) is raised by `load_session` for unknown ids.
- S04: test_architecture.py also checks that module-level imports point inward (providers/runtime below tools, tools below agent, agent below team/pipeline, pipeline below cli/tui/api). Call-time imports are only checked for the forbidden core list.
- S05: the OpenAI-compatible adapter speaks HTTP + SSE with `httpx` directly instead of the `openai` SDK: one transparent code path for every compatible server, simple typing, trivial `respx` mocks. The `openai` package is therefore not added.
- S05: a bad_request before any output is retried once with a minimal body (no `stream_options`, `response_format`, `reasoning_effort`), because many compatible servers reject optional fields.
- S05: unparsable tool-call arguments become `{"_raw_arguments": "..."}` so the tool framework can report them to the model instead of crashing.
- S05: provider objects are cached per config in `ForgeConfig.instances` (a private attribute), so `get_provider` keeps its contract signature without global state; `register_provider` injects a FakeProvider.
- S05: a retry is only attempted before the first streamed item (text already shown cannot be taken back); `Retry-After` above 60 s is not waited for.
- S05: helper modules `providers/sse.py`, `retry.py`, `tokens.py` and a minimal `catalog.py` (filled in S21) keep each file to one job.
- S06: `ToolDef` gains one optional trailing field `args_model` (the Pydantic model that validates arguments; None for MCP tools that only have a JSON schema). All contract fields are unchanged.
- S06: tool helpers raise `runtime.errors.ToolError` for expected failures; `call_tool` converts it to `ToolResult(ok=False)` in the `error[<code>]: ...` format. Tools themselves still return values to the model; this only keeps helper chains short.
- S06: an unexpected exception inside a tool is logged and returned as `tool_error` (internal error) instead of ending the session.
- S06: every call is appended to `.forge/audit.log` as one JSON line (tool, shortened args, ok, code, size, decision).
- S06: `HookEvent` in hooks.py is an alias of `config.HookEventName`, so config can validate hook names without importing hooks (avoids an import cycle).
- S06: test helpers (`ScriptedRenderer`, `NoExecutor`, `init_repo`, `make_ctx`) live in `tests/support.py`; `tests/conftest.py` holds the shared fixtures.
- S07: extra runtime modules keep each job separate: `edit.py` (exact replacement), `readers.py` (numbered text, images, PDF, notebooks), `search.py` (grep), `tree.py` (list_dir), `proc.py` (trusted helper programs: git, rg, pdftotext).
- S07: trusted helper programs (git ls-files, rg, pdftotext) run through `runtime/proc.py` directly; only commands the model asks for go through the Executor and its sandbox.
- S07: hidden (dot) paths are left out of list_dir, glob and grep by default, like ripgrep; `show_hidden`/`include_ignored` include them, and a glob that names a dot folder (`.github/**`) finds it.
- S07: `apply_changes` journals the previous content of every file to `.forge/undo/<session>/journal.jsonl` and rolls back already-written files if a later write fails.
- S07: edit_file matches against the original text (CRLF kept) and writes it back without re-normalizing line endings, so files with mixed endings only change where edited.
- S07: ruff allows long lines in `tools.py` and `prompts.py`, whose docstrings and prompts are model-facing text copied word for word from docs/TOOLS.md.
- S08: bash receives each command through a quoted heredoc and `eval`, so an unbalanced quote or `exit` can never swallow the sentinel; a shell that exits is restarted on the next command.
- S08: PowerShell receives each command base64-encoded on one line and runs it with `Invoke-Expression`; output goes through `Out-String` so objects are formatted before the sentinel, and `$PSStyle.OutputRendering = 'PlainText'` plus TERM=dumb keep escape codes out.
- S08: LocalExecutor keeps a pool of persistent shells per kind (a busy shell is never shared); every command starts with an explicit `cd` to `ctx.cwd`, so parallel agents stay correct.
- S08: `job_output` paging is 0-based throughout: `next since_line` equals the number of lines read so far (TOOLS.md's example shows one more, which would skip a line).
- S08: on Windows, Git Bash is preferred over WSL's `System32\\bash.exe`; Git Bash paths like `/c/x` are converted back to `C:/x`.
- S08: PowerShell 7 was installed in the dev container (from packages.microsoft.com) so the PowerShell paths are tested locally, not only in CI.
- S09: `prompts.py` was started in S09 (BASE, CODER, `render()`), because the loop needs `prompts.render(role)` and no prompt text may live anywhere else; S10/S11 extend it.
- S09: on a ProviderError the loop publishes an ErrorEvent naming `provider/model` and tries the next model of the role's chain; when all fail it stops with `stopped=\"error\"`.
- S09: the main agent's messages are appended to `ctx.session.messages` (the full transcript); saving the session is the caller's job (pipeline/CLI).
- S09: cancellation is not swallowed: a cancelled agent task raises CancelledError to whoever awaits it.
- S10: `src/forge/wiring.py` builds a Ctx from local ports (MemoryStore until S19, MemoryBus, LocalExecutor) and holds `use_fake_provider`; cli, tui and api share it.
- S10: `--fake` takes an optional `.json` script (`--fake` alone uses tests/fixtures/fake/hello.json, or a built-in hello turn outside the repo); every role then maps to `fake/<role>`, so scripts can keep per-role queues.
- S10: `-y/--yes` approves every tool call and takes default answers, so scripted and e2e runs need no stdin.
- S10: questions and approvals wait one short tick so events published before them are printed first.
- S11: every prompt is a (static text, volatile template) pair: render() returns static + model override + filled slots, so the cacheable prefix never changes within a PROMPTS_VERSION.
- S11: SAFETY is split out of BASE so non-coding roles (refiner, reviewer, compressor) share the safety rules without the coder's working style; FINAL_REVIEW was added for S20.
- S11: render() takes a keyword-only `model` to pick OVERRIDES by family (gemini, local models).
- S12: the agent loop renders memory into every system prompt; a cwd outside the root only loads root-level files.
- S13: plan.py also offers `Plan.step(id)` and `checklist(plan)` (the `[x] s1 Title` view used by prompts, renderers and tools).
- S14: `structured.py` reads a JSON object from raw text, a ```json fence or prose, and validates it into a Pydantic model.
- S14: `agent.complete()` is the tool-free model call (refine, review, compress) using the role's fallback chain.
- S15: `questions.py` holds the shared ask/headless-default logic for both ask_user and clarify; a renderer returning no answers means the user dismissed the questions.
- S15: headless clarify closes questions with defaults as assumptions without another refiner call; leftover questions after the round limit are closed the same way.
- S15: before a spec exists, headless answers are kept in `ctx.state.notes`.
- S16: the planner's system prompt gets the spec through the {spec} slot, filled from `ctx.session.spec`; the agent loop fills spec/plan/failure slots from the session.
- S16: rejecting a plan with feedback makes the planner revise it; rejecting without feedback ends planning with `PlanRejected`.
- S16: when the role is `replanner`, submit_plan keeps the done/skipped steps and numbers the new ones after them.
- S17: model calls moved from agent.py to `modelcall.py` (below tools), so `checks.py` can ask the reviewer without tools importing the agent loop upward; agent.py re-exports `complete`.
- S17: `checks.py` owns verification (`verify_step`, `settle_step`, `sandbox_policy`); `pipeline.verify_step` delegates to it, as the step card names it in pipeline.py.
- S17: check commands run with bash on POSIX and PowerShell on Windows, from the project root, timeout 600 s.
- S17: a replan keeps done/skipped steps; new step ids continue after the highest old id; at most 3 replans per execution.
- S18: snapshot commits use `commit-tree --no-gpg-sign` with a Forge identity, so they work in repos that sign commits and never need the user's identity.
- S18: the pipeline takes the snapshot in `before_step`; a failing snapshot is reported as an ErrorEvent and the step still runs.
- S18: /undo resets the rolled-back step to todo; without checkpoints it falls back to the apply_changes journal.
- S19: SqliteStore keeps each session as one JSON document plus an FTS5 table for recall; search quotes every query word, so FTS operators in user text are harmless (MemoryStore matches the same words).
- S19: the dependency is `sqlalchemy[asyncio]` (the extra brings greenlet, which SQLAlchemy's async engine needs).
- S19: the CLI's default store is now SqliteStore at `~/.forge/forge.db`; resume restarts an interrupted 'doing' step from scratch.
- S19: closing the executor stops a shell that was interrupted mid-command as a whole process tree.
- S20: `Report` lives in pipeline.py (where the contract puts it); `report_text()` renders it for SessionDone and the terminal.
- S20: eval tasks are TOML: repo, prompt, check (`{python}` = current interpreter) and an optional `[fake]` table of edits/writes from which the offline FakeProvider script is generated.
- S20: each eval runs in a fresh committed copy of its repo with a MemoryStore and `AutoRenderer` (approves everything, default answers).
- S20: `forge \"<prompt>\"` now runs `run_task`; the fake fixtures include a refiner answer.
- S21: a role entry naming a provider without a [providers.*] entry uses the catalog preset (base URL + API-key env var); a config entry always wins.
- S21: catalog prices are vendor list prices per 1M tokens at the time of writing (Claude values from the Anthropic model table of 2026-09-25); `[models.*]` overrides any of them.
- S22: S22: Anthropic adapter tests inject an httpx2 client (SDK 1.x uses httpx2, so respx cannot mock it); fixtures are recorded SSE event lists.
- S22: S22: Bedrock via base_url 'bedrock://<region>' (AnthropicBedrockMantle), Vertex via 'vertex://<project>/<region>'.
- S22: S22: ChatRequest.json_schema is not sent to Claude; structured.py's prompt + parse path covers it.
- S23: S23: Gemini assistant turns are replayed from the raw parts kept in Message.reasoning (keeps thought signatures); rebuilt from text/tool_calls when absent.
- S23: S23: reasoning_effort maps to thinking_budget (1024/8192/24576) on Gemini 2.5 and thinking_level LOW/HIGH on later models; json_schema is sent only when no tools are given.
- S24: S24: Responses wire lives in providers/responses.py (body, input items, event parsing), outside the card's file list, to keep openai_compat.py focused; requests are stateless (store=false) and encrypted reasoning items round-trip via Message.reasoning.
- S25: S25: the tool fallback wraps the provider in modelcall.model_turn (with_tool_fallback) when the request has tools and capabilities(model).tools is false; malformed JSON gets one corrective FIX_JSON turn, then the reply is passed on as plain text.
- S25: S25: LiteLLM is imported lazily with LITELLM_LOCAL_MODEL_COST_MAP=True so it never downloads its price table.
- S26a: S26: split into S26a (apply_patch, remember, recall) and S26b (repo_map, web_fetch, web_search).
- S26a: S26a: fuzzy hunk matches keep the file's own context lines; per-path permission checks for apply_patch arrive with the permission engine in S28 (paths already pass outside_root/protected_path).
- S26a: S26a: remember with scope=user writes ~/.forge/FORGE.md directly (outside the writable roots, so not covered by /undo).
- S26b: S26b: repo_map ranks by word occurrences in other files (regex identifiers), not by tree-sitter identifier nodes; grammars are downloaded by tree-sitter-language-pack on first use (offline: language counts as unsupported).
- S26b: S26b: HTML cleanup removes script/style/nav/footer/header/noscript/iframe by regex before markdownify, so beautifulsoup4 (a markdownify dependency) is not imported directly.
- S26b: S26b: native web_search is an optional provider method search_web(); only the Anthropic adapter has it (web_search_20250305 server tool). For searxng the variable named by search_api_key_env holds the instance URL.
- S26b: S26b: SessionState gained web_searches (per-session search counter).
- S27: S27: input budget = context_window - min(max_output, window/8) of the role's first model; level 2 keeps recent turns up to 30% of the budget, starting at an assistant message; summaries are capped at 20% of the budget.
- S27: S27: the compacted history is one user message of tagged sections (<spec>, <plan>, <summary>, <unresolved_errors>, <files_touched>, <task>); an error is unresolved while the same tool has not succeeded since.
- S27: S27: /compact sets ctx.state.compact_request for the next turn; /context reads ctx.state.context_usage recorded on every turn. COMPRESS_TASK prompt added.
- S28: S28: Linux prefers Landlock (ctypes, applied in preexec_fn; TCP blocked with ABI>=4), bubblewrap as fallback; macOS uses sandbox-exec with an allow-default profile that denies writes outside the writable folders and all network.
- S28: S28: read-only mode lets commands write only to Forge's scratch folder (<tmp>/forge-scratch) and /dev; workspace-write adds the writable roots and the temp folders.
- S28: S28: on approval policy on-request, bash/powershell run without asking when an OS sandbox is active, else ask; a command flagged sandbox_denied is offered for one rerun without the sandbox (never: reported as sandbox_denied).
- S28: S28: persistent shells are pooled per sandbox (Launch.key), since a sandbox cannot be lifted from a running process.
- S29: S29: the TUI keeps one session (ctx) for its lifetime; each prompt runs run_task on it. /jobs reads the executor's jobs by duck typing (the Executor port has no listing method).
- S30: S30: tools.py cannot import team.py (inward rule), so spawn_agent reaches the registry through ctx.state.team (a Team protocol in ctx.py) set by wiring.open_session.
- S30: S30: call_tool refuses tools outside agent_tools(ctx, ctx.role) with 'unsupported', so role limits hold even if a model calls a tool it was not offered.
- S31: S31: a custom role is installed into the session on spawn: model -> cfg.roles[name], tools and prompt -> ctx.state.custom_roles; its system prompt is TEAM_MEMBER plus the file body (CUSTOM_AGENT_TAIL). tools may name tools or groups.
- S32: S32: two events added beyond the contract: AgentMessage and AgentFinished. A lead that answers without tool calls while its background agents run waits for the next inbox message instead of ending.
- S32: S32: the agent-file loader moved from team.py to agent_files.py (split by responsibility; team.py grows with S33/S34).
- S32: S32: the main agent is a registry entry too (inbox, turns, usage); run_agent records every turn via team.record_turn.
- S33: S33: owners live behind a new BoardStore port (claim/release/owners), implemented by MemoryStore and SqliteStore; the board logic lives in board.py (team.py delegates) to keep team.py small.
- S33: S33: board tools are offered only when ctx.state.team_mode is true (S35 sets it); stop_agent gives an agent's unfinished tasks back to the board.
- S34: S34: worktrees start from a snapshot commit of the live tree (uncommitted work included); merges use git merge-tree --write-tree (needs git 2.38+) against a fresh snapshot and write only changed files, so the user's branch, HEAD and index never change. .forge/ is added to .git/info/exclude.
- S34: S34: board tasks (update_task done) are merged and reviewed (reviewer on the merge diff) before the lead is told; a conflict sets the task back to doing and tells the owner to run git merge forge/<session>/main in its worktree. Worktrees of merged agents are removed at session end (close_session); conflicts and stop_agent(keep_worktree) keep them.
- S35: S35: mode is chosen per task in run_task (ctx.state.mode). solo removes the agent tools from the lead; subagents and team give the main coder the TEAM_LEAD prompt; team mode starts up to max_parallel_agents background workers (TEAM_TASK prompt, worktrees in git repos) and runs leftover steps solo.
- S35: S35: the budget is the session total (ctx.state.usage, shared by every agent); every agent stops before a new turn once it is spent, execute stops between steps, and run_task returns a 'Stopped: the cost budget ... was used up' report.
- S36: S36: MCP tools live in a per-session McpHub (ctx.state.mcp, McpTools protocol), not in the global REGISTRY; call_tool falls back to it. Each server connection runs in its own task; server stderr goes to .forge/mcp/<server>.log. Rules accept * in the tool name (mcp__github__*).
- S36: S36: run_agent rebuilds the tool list every turn (tool_search adds tools mid-run). Deferred tools are listed in the system prompt via the DEFERRED_TOOLS prompt and the {deferred_tools} slot.
- S36: S36: forge eval --compare solo,team runs every task per mode and exits 0 only if the last mode passes more tasks than the first.
- S37: S37: hook commands run in bash (Git Bash on Windows) or PowerShell when no bash exists; {name} placeholders come from the tool arguments, then the event, shell-quoted. match is re.fullmatch on the tool name and is ignored for events without a tool.
- S37: S37: session_start runs in open_session; prompt_submit runs first in run_task and may stop the task; stop runs after the report is saved.
- S38: S38: a skill without front matter uses its folder name and first heading/line as description; unreadable skills are skipped.
- S39: S39: handle_command returns SlashResult (text, a prompt to run as a task, or resume); run_command stays the text-only form. Custom commands: project overrides user; arguments are appended when a template has no $ARGUMENTS.
- S40: S40: the JSON stream holds only the contract event kinds (agent_message/agent_finished are left out). Without --yes every approval is refused with feedback for the model. --no-defaults: a question ends the run with exit 2 and a SessionDone report 'needs input' (questions from tools cancel the task; questions from the pipeline raise NeedsInput).
- S41: S41: without a renderer the API runs headless with AutoRenderer (approve=True, like forge run --yes) or a refusing renderer (approve=False). forge.Forge is exported lazily (module __getattr__) so the CLI does not import the pipeline at startup.
- S41: S41: changed_files never lists Forge's own .forge/ files.
- S42: S42: config warnings (e.g. ignored untrusted [providers]/[hooks]/[mcp_servers]) are printed to stderr by every CLI command, not only config check.
- S43: S43: the search ranking moved into memory_store.rank_sessions, shared by MemoryStore and JsonStore. The renderer suite covers the non-interactive renderers (Auto, Json, Rich with auto-approve); the TUI renderer is covered by its pilot tests.
- S44: S44: every eval task is generated and verified by a script: the check fails on the original repo and passes after the task's [fake] solution. Checks avoid $ and backslash escapes that bash and PowerShell treat differently.
- S44: S44: eval commands moved from cli.py to eval_cli.py (cli.py was nearing 500 lines); the SWE-bench runner lives in swebench.py. PROMPTS_VERSION bumped to 2026.10.4 (prompts changed in S30-S38).
- S44: S43 fix: .gitignore ignored examples/plugin_demo/.forge/, so CI lacked the demo config; it is now re-included.
- S45: S45: $FORGE_BASH / $FORGE_POWERSHELL (path or 'none') pick the shell executables; they are reserved and not read as config keys. actions/checkout moved to v5 (Node 24).
- S46: S46: command rules see every simple command in a line (quote-aware split on ; | & && || newline, $(...) and backticks, sh -c / pwsh -Command, sudo/env/nohup/xargs/VAR= wrappers): deny/ask match if any part matches, allow only if all parts are allowed. A bash(...) rule also covers powershell and vice versa (the S28 rule-matrix case was changed accordingly; it is stricter).
- S46: S46: reading a path outside the project and writable roots (symlinks resolved) asks first; ~/.forge/skills is exempt because agents are told to read skills there. Every tool result is secret-masked before capping.
- S47: S47: docs/config.md is generated by `forge config schema --markdown`; test_docs fails when it drifts or a field has no description.
- S48: S48: PyInstaller is a build tool only (`uv run --with pyinstaller` in the release workflow), not a project dependency. The spec collects forge, textual, tree-sitter-language-pack and the litellm/mcp client modules explicitly (they load lazily) and skips litellm.proxy, mcp.cli and mcp.server, which need extras.
- S48: S48: the release workflow can be run by hand (workflow_dispatch) to build and smoke-test on all three OS without publishing.
- S49: S49: research reuses team.spawn (foreground researcher, depth normal=15 / deep=40 turns) and is in LEAD_ONLY_TOOLS, so sub-agents never start agents; it is not in AGENT_TOOLS, so solo mode keeps it.
- S49: S49: native web search now uses the calling agent's model (falls back to the coder chain via resolve_role); RESEARCH_RULES are part of the CODER and TEAM_LEAD prompts. PROMPTS_VERSION 2026.10.6.
- S50: S50: the browser factory and the open browsers live in SessionState (like team and mcp), not as a new Ctx field; one Chromium per session, one fresh context per agent, closed when the agent finishes.
- S50: S50: the Playwright context aborts every request to local or private hosts (DNS-checked, cached per host); browser_open additionally runs checked_url and asks like web_fetch; the other actions are auto.
- S50: S50: [browser] channel/executable select an installed browser; tests read FORGE_BROWSER__EXECUTABLE, and CI sets FORGE_REQUIRE_BROWSER so the conformance suite cannot silently skip there.
- S51: S51: Executor.run takes a synchronous on_output(str) callback; the bash/powershell tool body buffers it in LiveOutput and publishes from its own task, so the executor never awaits the bus.
- S51: S51: the running call's id reaches tool bodies through a ContextVar (CALL_ID) set in _run_body; it is task-local, not shared state.
- S51: S51: the persistent shell now holds back only a possible marker prefix instead of 4 KB, so short output passes at once; stderr still goes to a temp file and appears only in the result (live view shows stdout).
- S52: S52: early start only while every earlier call of the reply was also early-startable, so a read never overtakes a write that the model asked for first.
- S52: S52: fixed an S50 bug on the way: browser tools are read-only but stateful, so read-only parallelism now uses can_run_concurrently (excludes the browser group).
- S52: S52: FakeTurn gained delay_s and error-after-calls to script slow and failing streams.
- S53: S53: monitors poll the job log through Executor.job_output (no port change); messages go through the team inbox, so delivery and waiting reuse the background-agent path.
- S53: S53: monitor is a command tool for the permission rules (bash/powershell rules cover it). PROMPTS_VERSION 2026.10.7.
- S54: S54: todos live in SessionState (per agent) and are not saved with the session; a resumed session continues from the plan, which is the durable record. PROMPTS_VERSION 2026.10.8.
- S55: S55: tasks_view reads the local executor's jobs and the registry's agents with getattr (like /jobs did), so other executors simply show no jobs; background shell jobs and monitors record a label in SessionState.job_labels.
- S56: S56: config edits work on the TOML text and replace only [mcp_servers.<name>] and its sub-tables, so comments and other settings stay; the result is parsed again and refused if the entry is not exactly what was written.
- S56: S56: add-json accepts the Claude Code / Desktop entry format; env values and fixed header values are never stored, only variable names (env_keys, headers_env from ${VAR}).

- S28 (CI fix): read-only sandboxes set TMPDIR/TMP/TEMP to Forge's scratch folder, because macOS bash 3.2 writes here-documents to $TMPDIR.
- S30: S30: tools.py cannot import team.py (inward rule), so spawn_agent reaches the registry through ctx.state.team (a Team protocol in ctx.py) set by wiring.open_session.
- S30: S30: call_tool refuses tools outside agent_tools(ctx, ctx.role) with 'unsupported', so role limits hold even if a model calls a tool it was not offered.
- S31: S31: a custom role is installed into the session on spawn: model -> cfg.roles[name], tools and prompt -> ctx.state.custom_roles; its system prompt is TEAM_MEMBER plus the file body (CUSTOM_AGENT_TAIL). tools may name tools or groups.
- S32: S32: two events added beyond the contract: AgentMessage and AgentFinished. A lead that answers without tool calls while its background agents run waits for the next inbox message instead of ending.
- S32: S32: the agent-file loader moved from team.py to agent_files.py (split by responsibility; team.py grows with S33/S34).
- S32: S32: the main agent is a registry entry too (inbox, turns, usage); run_agent records every turn via team.record_turn.
- S33: S33: owners live behind a new BoardStore port (claim/release/owners), implemented by MemoryStore and SqliteStore; the board logic lives in board.py (team.py delegates) to keep team.py small.
- S33: S33: board tools are offered only when ctx.state.team_mode is true (S35 sets it); stop_agent gives an agent's unfinished tasks back to the board.
- S34: S34: worktrees start from a snapshot commit of the live tree (uncommitted work included); merges use git merge-tree --write-tree (needs git 2.38+) against a fresh snapshot and write only changed files, so the user's branch, HEAD and index never change. .forge/ is added to .git/info/exclude.
- S34: S34: board tasks (update_task done) are merged and reviewed (reviewer on the merge diff) before the lead is told; a conflict sets the task back to doing and tells the owner to run git merge forge/<session>/main in its worktree. Worktrees of merged agents are removed at session end (close_session); conflicts and stop_agent(keep_worktree) keep them.
- S35: S35: mode is chosen per task in run_task (ctx.state.mode). solo removes the agent tools from the lead; subagents and team give the main coder the TEAM_LEAD prompt; team mode starts up to max_parallel_agents background workers (TEAM_TASK prompt, worktrees in git repos) and runs leftover steps solo.
- S35: S35: the budget is the session total (ctx.state.usage, shared by every agent); every agent stops before a new turn once it is spent, execute stops between steps, and run_task returns a 'Stopped: the cost budget ... was used up' report.
- S36: S36: MCP tools live in a per-session McpHub (ctx.state.mcp, McpTools protocol), not in the global REGISTRY; call_tool falls back to it. Each server connection runs in its own task; server stderr goes to .forge/mcp/<server>.log. Rules accept * in the tool name (mcp__github__*).
- S36: S36: run_agent rebuilds the tool list every turn (tool_search adds tools mid-run). Deferred tools are listed in the system prompt via the DEFERRED_TOOLS prompt and the {deferred_tools} slot.
- S36: S36: forge eval --compare solo,team runs every task per mode and exits 0 only if the last mode passes more tasks than the first.
- S37: S37: hook commands run in bash (Git Bash on Windows) or PowerShell when no bash exists; {name} placeholders come from the tool arguments, then the event, shell-quoted. match is re.fullmatch on the tool name and is ignored for events without a tool.
- S37: S37: session_start runs in open_session; prompt_submit runs first in run_task and may stop the task; stop runs after the report is saved.
- S38: S38: a skill without front matter uses its folder name and first heading/line as description; unreadable skills are skipped.
- S39: S39: handle_command returns SlashResult (text, a prompt to run as a task, or resume); run_command stays the text-only form. Custom commands: project overrides user; arguments are appended when a template has no $ARGUMENTS.
- S40: S40: the JSON stream holds only the contract event kinds (agent_message/agent_finished are left out). Without --yes every approval is refused with feedback for the model. --no-defaults: a question ends the run with exit 2 and a SessionDone report 'needs input' (questions from tools cancel the task; questions from the pipeline raise NeedsInput).
- S41: S41: without a renderer the API runs headless with AutoRenderer (approve=True, like forge run --yes) or a refusing renderer (approve=False). forge.Forge is exported lazily (module __getattr__) so the CLI does not import the pipeline at startup.
- S41: S41: changed_files never lists Forge's own .forge/ files.
- S42: S42: config warnings (e.g. ignored untrusted [providers]/[hooks]/[mcp_servers]) are printed to stderr by every CLI command, not only config check.
- S43: S43: the search ranking moved into memory_store.rank_sessions, shared by MemoryStore and JsonStore. The renderer suite covers the non-interactive renderers (Auto, Json, Rich with auto-approve); the TUI renderer is covered by its pilot tests.
- S44: S44: every eval task is generated and verified by a script: the check fails on the original repo and passes after the task's [fake] solution. Checks avoid $ and backslash escapes that bash and PowerShell treat differently.
- S44: S44: eval commands moved from cli.py to eval_cli.py (cli.py was nearing 500 lines); the SWE-bench runner lives in swebench.py. PROMPTS_VERSION bumped to 2026.10.4 (prompts changed in S30-S38).
- S44: S43 fix: .gitignore ignored examples/plugin_demo/.forge/, so CI lacked the demo config; it is now re-included.
- S45: S45: $FORGE_BASH / $FORGE_POWERSHELL (path or 'none') pick the shell executables; they are reserved and not read as config keys. actions/checkout moved to v5 (Node 24).
- S46: S46: command rules see every simple command in a line (quote-aware split on ; | & && || newline, $(...) and backticks, sh -c / pwsh -Command, sudo/env/nohup/xargs/VAR= wrappers): deny/ask match if any part matches, allow only if all parts are allowed. A bash(...) rule also covers powershell and vice versa (the S28 rule-matrix case was changed accordingly; it is stricter).
- S46: S46: reading a path outside the project and writable roots (symlinks resolved) asks first; ~/.forge/skills is exempt because agents are told to read skills there. Every tool result is secret-masked before capping.
- S47: S47: docs/config.md is generated by `forge config schema --markdown`; test_docs fails when it drifts or a field has no description.
- S48: S48: PyInstaller is a build tool only (`uv run --with pyinstaller` in the release workflow), not a project dependency. The spec collects forge, textual, tree-sitter-language-pack and the litellm/mcp client modules explicitly (they load lazily) and skips litellm.proxy, mcp.cli and mcp.server, which need extras.
- S48: S48: the release workflow can be run by hand (workflow_dispatch) to build and smoke-test on all three OS without publishing.
- S49: S49: research reuses team.spawn (foreground researcher, depth normal=15 / deep=40 turns) and is in LEAD_ONLY_TOOLS, so sub-agents never start agents; it is not in AGENT_TOOLS, so solo mode keeps it.
- S49: S49: native web search now uses the calling agent's model (falls back to the coder chain via resolve_role); RESEARCH_RULES are part of the CODER and TEAM_LEAD prompts. PROMPTS_VERSION 2026.10.6.
- S50: S50: the browser factory and the open browsers live in SessionState (like team and mcp), not as a new Ctx field; one Chromium per session, one fresh context per agent, closed when the agent finishes.
- S50: S50: the Playwright context aborts every request to local or private hosts (DNS-checked, cached per host); browser_open additionally runs checked_url and asks like web_fetch; the other actions are auto.
- S50: S50: [browser] channel/executable select an installed browser; tests read FORGE_BROWSER__EXECUTABLE, and CI sets FORGE_REQUIRE_BROWSER so the conformance suite cannot silently skip there.
- S51: S51: Executor.run takes a synchronous on_output(str) callback; the bash/powershell tool body buffers it in LiveOutput and publishes from its own task, so the executor never awaits the bus.
- S51: S51: the running call's id reaches tool bodies through a ContextVar (CALL_ID) set in _run_body; it is task-local, not shared state.
- S51: S51: the persistent shell now holds back only a possible marker prefix instead of 4 KB, so short output passes at once; stderr still goes to a temp file and appears only in the result (live view shows stdout).
- S52: S52: early start only while every earlier call of the reply was also early-startable, so a read never overtakes a write that the model asked for first.
- S52: S52: fixed an S50 bug on the way: browser tools are read-only but stateful, so read-only parallelism now uses can_run_concurrently (excludes the browser group).
- S52: S52: FakeTurn gained delay_s and error-after-calls to script slow and failing streams.
- S53: S53: monitors poll the job log through Executor.job_output (no port change); messages go through the team inbox, so delivery and waiting reuse the background-agent path.
- S53: S53: monitor is a command tool for the permission rules (bash/powershell rules cover it). PROMPTS_VERSION 2026.10.7.
- S54: S54: todos live in SessionState (per agent) and are not saved with the session; a resumed session continues from the plan, which is the durable record. PROMPTS_VERSION 2026.10.8.
- S55: S55: tasks_view reads the local executor's jobs and the registry's agents with getattr (like /jobs did), so other executors simply show no jobs; background shell jobs and monitors record a label in SessionState.job_labels.
- S56: S56: config edits work on the TOML text and replace only [mcp_servers.<name>] and its sub-tables, so comments and other settings stay; the result is parsed again and refused if the entry is not exactly what was written.
- S56: S56: add-json accepts the Claude Code / Desktop entry format; env values and fixed header values are never stored, only variable names (env_keys, headers_env from ${VAR}).

- S28 (CI fix 2): the persistent bash reads each command from stdin up to a NUL byte instead of a here-document; macOS bash 3.2 writes here-documents to /tmp regardless of TMPDIR, which the read-only sandbox forbids.
- S35: S35: mode is chosen per task in run_task (ctx.state.mode). solo removes the agent tools from the lead; subagents and team give the main coder the TEAM_LEAD prompt; team mode starts up to max_parallel_agents background workers (TEAM_TASK prompt, worktrees in git repos) and runs leftover steps solo.
- S35: S35: the budget is the session total (ctx.state.usage, shared by every agent); every agent stops before a new turn once it is spent, execute stops between steps, and run_task returns a 'Stopped: the cost budget ... was used up' report.
- S36: S36: MCP tools live in a per-session McpHub (ctx.state.mcp, McpTools protocol), not in the global REGISTRY; call_tool falls back to it. Each server connection runs in its own task; server stderr goes to .forge/mcp/<server>.log. Rules accept * in the tool name (mcp__github__*).
- S36: S36: run_agent rebuilds the tool list every turn (tool_search adds tools mid-run). Deferred tools are listed in the system prompt via the DEFERRED_TOOLS prompt and the {deferred_tools} slot.
- S36: S36: forge eval --compare solo,team runs every task per mode and exits 0 only if the last mode passes more tasks than the first.
- S37: S37: hook commands run in bash (Git Bash on Windows) or PowerShell when no bash exists; {name} placeholders come from the tool arguments, then the event, shell-quoted. match is re.fullmatch on the tool name and is ignored for events without a tool.
- S37: S37: session_start runs in open_session; prompt_submit runs first in run_task and may stop the task; stop runs after the report is saved.
- S38: S38: a skill without front matter uses its folder name and first heading/line as description; unreadable skills are skipped.
- S39: S39: handle_command returns SlashResult (text, a prompt to run as a task, or resume); run_command stays the text-only form. Custom commands: project overrides user; arguments are appended when a template has no $ARGUMENTS.
- S40: S40: the JSON stream holds only the contract event kinds (agent_message/agent_finished are left out). Without --yes every approval is refused with feedback for the model. --no-defaults: a question ends the run with exit 2 and a SessionDone report 'needs input' (questions from tools cancel the task; questions from the pipeline raise NeedsInput).
- S41: S41: without a renderer the API runs headless with AutoRenderer (approve=True, like forge run --yes) or a refusing renderer (approve=False). forge.Forge is exported lazily (module __getattr__) so the CLI does not import the pipeline at startup.
- S41: S41: changed_files never lists Forge's own .forge/ files.
- S42: S42: config warnings (e.g. ignored untrusted [providers]/[hooks]/[mcp_servers]) are printed to stderr by every CLI command, not only config check.
- S43: S43: the search ranking moved into memory_store.rank_sessions, shared by MemoryStore and JsonStore. The renderer suite covers the non-interactive renderers (Auto, Json, Rich with auto-approve); the TUI renderer is covered by its pilot tests.
- S44: S44: every eval task is generated and verified by a script: the check fails on the original repo and passes after the task's [fake] solution. Checks avoid $ and backslash escapes that bash and PowerShell treat differently.
- S44: S44: eval commands moved from cli.py to eval_cli.py (cli.py was nearing 500 lines); the SWE-bench runner lives in swebench.py. PROMPTS_VERSION bumped to 2026.10.4 (prompts changed in S30-S38).
- S44: S43 fix: .gitignore ignored examples/plugin_demo/.forge/, so CI lacked the demo config; it is now re-included.
- S45: S45: $FORGE_BASH / $FORGE_POWERSHELL (path or 'none') pick the shell executables; they are reserved and not read as config keys. actions/checkout moved to v5 (Node 24).
- S46: S46: command rules see every simple command in a line (quote-aware split on ; | & && || newline, $(...) and backticks, sh -c / pwsh -Command, sudo/env/nohup/xargs/VAR= wrappers): deny/ask match if any part matches, allow only if all parts are allowed. A bash(...) rule also covers powershell and vice versa (the S28 rule-matrix case was changed accordingly; it is stricter).
- S46: S46: reading a path outside the project and writable roots (symlinks resolved) asks first; ~/.forge/skills is exempt because agents are told to read skills there. Every tool result is secret-masked before capping.
- S47: S47: docs/config.md is generated by `forge config schema --markdown`; test_docs fails when it drifts or a field has no description.
- S48: S48: PyInstaller is a build tool only (`uv run --with pyinstaller` in the release workflow), not a project dependency. The spec collects forge, textual, tree-sitter-language-pack and the litellm/mcp client modules explicitly (they load lazily) and skips litellm.proxy, mcp.cli and mcp.server, which need extras.
- S48: S48: the release workflow can be run by hand (workflow_dispatch) to build and smoke-test on all three OS without publishing.
- S49: S49: research reuses team.spawn (foreground researcher, depth normal=15 / deep=40 turns) and is in LEAD_ONLY_TOOLS, so sub-agents never start agents; it is not in AGENT_TOOLS, so solo mode keeps it.
- S49: S49: native web search now uses the calling agent's model (falls back to the coder chain via resolve_role); RESEARCH_RULES are part of the CODER and TEAM_LEAD prompts. PROMPTS_VERSION 2026.10.6.
- S50: S50: the browser factory and the open browsers live in SessionState (like team and mcp), not as a new Ctx field; one Chromium per session, one fresh context per agent, closed when the agent finishes.
- S50: S50: the Playwright context aborts every request to local or private hosts (DNS-checked, cached per host); browser_open additionally runs checked_url and asks like web_fetch; the other actions are auto.
- S50: S50: [browser] channel/executable select an installed browser; tests read FORGE_BROWSER__EXECUTABLE, and CI sets FORGE_REQUIRE_BROWSER so the conformance suite cannot silently skip there.
- S51: S51: Executor.run takes a synchronous on_output(str) callback; the bash/powershell tool body buffers it in LiveOutput and publishes from its own task, so the executor never awaits the bus.
- S51: S51: the running call's id reaches tool bodies through a ContextVar (CALL_ID) set in _run_body; it is task-local, not shared state.
- S51: S51: the persistent shell now holds back only a possible marker prefix instead of 4 KB, so short output passes at once; stderr still goes to a temp file and appears only in the result (live view shows stdout).
- S52: S52: early start only while every earlier call of the reply was also early-startable, so a read never overtakes a write that the model asked for first.
- S52: S52: fixed an S50 bug on the way: browser tools are read-only but stateful, so read-only parallelism now uses can_run_concurrently (excludes the browser group).
- S52: S52: FakeTurn gained delay_s and error-after-calls to script slow and failing streams.
- S53: S53: monitors poll the job log through Executor.job_output (no port change); messages go through the team inbox, so delivery and waiting reuse the background-agent path.
- S53: S53: monitor is a command tool for the permission rules (bash/powershell rules cover it). PROMPTS_VERSION 2026.10.7.
- S54: S54: todos live in SessionState (per agent) and are not saved with the session; a resumed session continues from the plan, which is the durable record. PROMPTS_VERSION 2026.10.8.
- S55: S55: tasks_view reads the local executor's jobs and the registry's agents with getattr (like /jobs did), so other executors simply show no jobs; background shell jobs and monitors record a label in SessionState.job_labels.
- S56: S56: config edits work on the TOML text and replace only [mcp_servers.<name>] and its sub-tables, so comments and other settings stay; the result is parsed again and refused if the entry is not exactly what was written.
- S56: S56: add-json accepts the Claude Code / Desktop entry format; env values and fixed header values are never stored, only variable names (env_keys, headers_env from ${VAR}).

- S36 (perf): SqliteStore uses WAL with synchronous=NORMAL (fewer fsyncs; the board race test went from 6.5s to 4.6s); offline suite ~48s.
- S37: S37: hook commands run in bash (Git Bash on Windows) or PowerShell when no bash exists; {name} placeholders come from the tool arguments, then the event, shell-quoted. match is re.fullmatch on the tool name and is ignored for events without a tool.
- S37: S37: session_start runs in open_session; prompt_submit runs first in run_task and may stop the task; stop runs after the report is saved.
- S38: S38: a skill without front matter uses its folder name and first heading/line as description; unreadable skills are skipped.
- S39: S39: handle_command returns SlashResult (text, a prompt to run as a task, or resume); run_command stays the text-only form. Custom commands: project overrides user; arguments are appended when a template has no $ARGUMENTS.
- S40: S40: the JSON stream holds only the contract event kinds (agent_message/agent_finished are left out). Without --yes every approval is refused with feedback for the model. --no-defaults: a question ends the run with exit 2 and a SessionDone report 'needs input' (questions from tools cancel the task; questions from the pipeline raise NeedsInput).
- S41: S41: without a renderer the API runs headless with AutoRenderer (approve=True, like forge run --yes) or a refusing renderer (approve=False). forge.Forge is exported lazily (module __getattr__) so the CLI does not import the pipeline at startup.
- S41: S41: changed_files never lists Forge's own .forge/ files.
- S42: S42: config warnings (e.g. ignored untrusted [providers]/[hooks]/[mcp_servers]) are printed to stderr by every CLI command, not only config check.
- S43: S43: the search ranking moved into memory_store.rank_sessions, shared by MemoryStore and JsonStore. The renderer suite covers the non-interactive renderers (Auto, Json, Rich with auto-approve); the TUI renderer is covered by its pilot tests.
- S44: S44: every eval task is generated and verified by a script: the check fails on the original repo and passes after the task's [fake] solution. Checks avoid $ and backslash escapes that bash and PowerShell treat differently.
- S44: S44: eval commands moved from cli.py to eval_cli.py (cli.py was nearing 500 lines); the SWE-bench runner lives in swebench.py. PROMPTS_VERSION bumped to 2026.10.4 (prompts changed in S30-S38).
- S44: S43 fix: .gitignore ignored examples/plugin_demo/.forge/, so CI lacked the demo config; it is now re-included.
- S45: S45: $FORGE_BASH / $FORGE_POWERSHELL (path or 'none') pick the shell executables; they are reserved and not read as config keys. actions/checkout moved to v5 (Node 24).
- S46: S46: command rules see every simple command in a line (quote-aware split on ; | & && || newline, $(...) and backticks, sh -c / pwsh -Command, sudo/env/nohup/xargs/VAR= wrappers): deny/ask match if any part matches, allow only if all parts are allowed. A bash(...) rule also covers powershell and vice versa (the S28 rule-matrix case was changed accordingly; it is stricter).
- S46: S46: reading a path outside the project and writable roots (symlinks resolved) asks first; ~/.forge/skills is exempt because agents are told to read skills there. Every tool result is secret-masked before capping.
- S47: S47: docs/config.md is generated by `forge config schema --markdown`; test_docs fails when it drifts or a field has no description.
- S48: S48: PyInstaller is a build tool only (`uv run --with pyinstaller` in the release workflow), not a project dependency. The spec collects forge, textual, tree-sitter-language-pack and the litellm/mcp client modules explicitly (they load lazily) and skips litellm.proxy, mcp.cli and mcp.server, which need extras.
- S48: S48: the release workflow can be run by hand (workflow_dispatch) to build and smoke-test on all three OS without publishing.
- S49: S49: research reuses team.spawn (foreground researcher, depth normal=15 / deep=40 turns) and is in LEAD_ONLY_TOOLS, so sub-agents never start agents; it is not in AGENT_TOOLS, so solo mode keeps it.
- S49: S49: native web search now uses the calling agent's model (falls back to the coder chain via resolve_role); RESEARCH_RULES are part of the CODER and TEAM_LEAD prompts. PROMPTS_VERSION 2026.10.6.
- S50: S50: the browser factory and the open browsers live in SessionState (like team and mcp), not as a new Ctx field; one Chromium per session, one fresh context per agent, closed when the agent finishes.
- S50: S50: the Playwright context aborts every request to local or private hosts (DNS-checked, cached per host); browser_open additionally runs checked_url and asks like web_fetch; the other actions are auto.
- S50: S50: [browser] channel/executable select an installed browser; tests read FORGE_BROWSER__EXECUTABLE, and CI sets FORGE_REQUIRE_BROWSER so the conformance suite cannot silently skip there.
- S51: S51: Executor.run takes a synchronous on_output(str) callback; the bash/powershell tool body buffers it in LiveOutput and publishes from its own task, so the executor never awaits the bus.
- S51: S51: the running call's id reaches tool bodies through a ContextVar (CALL_ID) set in _run_body; it is task-local, not shared state.
- S51: S51: the persistent shell now holds back only a possible marker prefix instead of 4 KB, so short output passes at once; stderr still goes to a temp file and appears only in the result (live view shows stdout).
- S52: S52: early start only while every earlier call of the reply was also early-startable, so a read never overtakes a write that the model asked for first.
- S52: S52: fixed an S50 bug on the way: browser tools are read-only but stateful, so read-only parallelism now uses can_run_concurrently (excludes the browser group).
- S52: S52: FakeTurn gained delay_s and error-after-calls to script slow and failing streams.
- S53: S53: monitors poll the job log through Executor.job_output (no port change); messages go through the team inbox, so delivery and waiting reuse the background-agent path.
- S53: S53: monitor is a command tool for the permission rules (bash/powershell rules cover it). PROMPTS_VERSION 2026.10.7.
- S54: S54: todos live in SessionState (per agent) and are not saved with the session; a resumed session continues from the plan, which is the durable record. PROMPTS_VERSION 2026.10.8.
- S55: S55: tasks_view reads the local executor's jobs and the registry's agents with getattr (like /jobs did), so other executors simply show no jobs; background shell jobs and monitors record a label in SessionState.job_labels.
- S56: S56: config edits work on the TOML text and replace only [mcp_servers.<name>] and its sub-tables, so comments and other settings stay; the result is parsed again and refused if the entry is not exactly what was written.
- S56: S56: add-json accepts the Claude Code / Desktop entry format; env values and fixed header values are never stored, only variable names (env_keys, headers_env from ${VAR}).

- S37 (CI fix): a timed-out hook is killed as a process tree on Windows (taskkill /T /F); killing only bash left the child holding the pipes.
- S45: S45: $FORGE_BASH / $FORGE_POWERSHELL (path or 'none') pick the shell executables; they are reserved and not read as config keys. actions/checkout moved to v5 (Node 24).
- S46: S46: command rules see every simple command in a line (quote-aware split on ; | & && || newline, $(...) and backticks, sh -c / pwsh -Command, sudo/env/nohup/xargs/VAR= wrappers): deny/ask match if any part matches, allow only if all parts are allowed. A bash(...) rule also covers powershell and vice versa (the S28 rule-matrix case was changed accordingly; it is stricter).
- S46: S46: reading a path outside the project and writable roots (symlinks resolved) asks first; ~/.forge/skills is exempt because agents are told to read skills there. Every tool result is secret-masked before capping.
- S47: S47: docs/config.md is generated by `forge config schema --markdown`; test_docs fails when it drifts or a field has no description.
- S48: S48: PyInstaller is a build tool only (`uv run --with pyinstaller` in the release workflow), not a project dependency. The spec collects forge, textual, tree-sitter-language-pack and the litellm/mcp client modules explicitly (they load lazily) and skips litellm.proxy, mcp.cli and mcp.server, which need extras.
- S48: S48: the release workflow can be run by hand (workflow_dispatch) to build and smoke-test on all three OS without publishing.
- Phase 6: the user asked for S49–S56 after v1.0 (research and browser agents, tool streaming, monitor, todos, tasks view, MCP management); they approved `playwright` as a dependency and the contract changes these steps need (new events, `Executor.run(on_output)`, `StreamItem.tool_call`, `Browser` port).
- S49: S49: research reuses team.spawn (foreground researcher, depth normal=15 / deep=40 turns) and is in LEAD_ONLY_TOOLS, so sub-agents never start agents; it is not in AGENT_TOOLS, so solo mode keeps it.
- S49: S49: native web search now uses the calling agent's model (falls back to the coder chain via resolve_role); RESEARCH_RULES are part of the CODER and TEAM_LEAD prompts. PROMPTS_VERSION 2026.10.6.
- S50: S50: the browser factory and the open browsers live in SessionState (like team and mcp), not as a new Ctx field; one Chromium per session, one fresh context per agent, closed when the agent finishes.
- S50: S50: the Playwright context aborts every request to local or private hosts (DNS-checked, cached per host); browser_open additionally runs checked_url and asks like web_fetch; the other actions are auto.
- S50: S50: [browser] channel/executable select an installed browser; tests read FORGE_BROWSER__EXECUTABLE, and CI sets FORGE_REQUIRE_BROWSER so the conformance suite cannot silently skip there.
- S51: S51: Executor.run takes a synchronous on_output(str) callback; the bash/powershell tool body buffers it in LiveOutput and publishes from its own task, so the executor never awaits the bus.
- S51: S51: the running call's id reaches tool bodies through a ContextVar (CALL_ID) set in _run_body; it is task-local, not shared state.
- S51: S51: the persistent shell now holds back only a possible marker prefix instead of 4 KB, so short output passes at once; stderr still goes to a temp file and appears only in the result (live view shows stdout).
- S52: S52: early start only while every earlier call of the reply was also early-startable, so a read never overtakes a write that the model asked for first.
- S52: S52: fixed an S50 bug on the way: browser tools are read-only but stateful, so read-only parallelism now uses can_run_concurrently (excludes the browser group).
- S52: S52: FakeTurn gained delay_s and error-after-calls to script slow and failing streams.
- S53: S53: monitors poll the job log through Executor.job_output (no port change); messages go through the team inbox, so delivery and waiting reuse the background-agent path.
- S53: S53: monitor is a command tool for the permission rules (bash/powershell rules cover it). PROMPTS_VERSION 2026.10.7.
- S54: S54: todos live in SessionState (per agent) and are not saved with the session; a resumed session continues from the plan, which is the durable record. PROMPTS_VERSION 2026.10.8.
- S55: S55: tasks_view reads the local executor's jobs and the registry's agents with getattr (like /jobs did), so other executors simply show no jobs; background shell jobs and monitors record a label in SessionState.job_labels.
- S56: S56: config edits work on the TOML text and replace only [mcp_servers.<name>] and its sub-tables, so comments and other settings stay; the result is parsed again and refused if the entry is not exactly what was written.
- S56: S56: add-json accepts the Claude Code / Desktop entry format; env values and fixed header values are never stored, only variable names (env_keys, headers_env from ${VAR}).
- S56 fix: on Windows, /mcp arguments are split without POSIX escaping (shlex posix=False, outer quotes removed), so backslashes in paths stay.

## Open issues
- S05: `Provider.stream` is declared `def stream(...) -> AsyncIterator[StreamItem]` in the Protocol instead of `async def`: implementations are async generators, and mypy only matches those against a plain `def` returning an iterator. Callers use it exactly as the contract shows (`async for item in provider.stream(req)`).
- S07: TOOLS.md asks read_file to downscale images to 1568 px; no image library is in the dependency list, so images are sent as they are (dimensions read from the file header) and refused above 5 MB. Adding Pillow would allow downscaling.
- S07: TOOLS.md says reads outside the project need approval; that is a permission rule and is implemented with the full permission engine in S28.
- S08: `CommandResult` gained four optional fields that the shell and job tools need and the contract lacks: `cwd` (folder after a shell command), `pid`, `elapsed_s` and `total_lines` (job status and paging). `ports.JobNotFoundError` was added for unknown job ids. All contract fields are unchanged.
- S14: `Ctx` gained one field beyond the contract, `state: SessionState` (default factory): runtime state shared by all agents of a session — the usage tally now, the agent registry and budgets later. All contract fields are unchanged.
- S22: S22: live contract test (tests/contract) not run here: no API keys in this environment.
- S23: S23: live contract test for Gemini not run here: no GEMINI_API_KEY.
- S25: S25: Phase 2 gate (10 providers pass the live contract test) not verified here: no API keys in this environment. 12 live cases exist in tests/contract and skip without keys; run 'uv run pytest tests/contract -m live' with keys set.
- S26b: S26b: native web search for OpenAI/Gemini providers not implemented (they fall back to unsupported unless an HTTP backend is configured).
- S28: S28: Windows has no OS sandbox yet (restricted token + job object not implemented); there, commands rely on approvals (on-request asks for every non-read-only command) and Forge's path checks. Sandbox tests skip on Windows.
- S28: S28: Landlock cannot protect .git/.forge inside a writable root (allow-only rules); only Forge's own file tools enforce protected_path. sandbox_denied detection is a heuristic on error text.
- S29: S29: Phase 2 gate also needs the live provider contract run (see S25); everything else in Phase 2 is verified offline.
- S30: S30: sub-agent transcripts are kept in AgentRegistry (memory), not yet saved to the store under the agent id as docs/TOOLS.md says.
- S36: S36: Phase 3 gate (team passes more large tasks than solo) needs live models; offline (--fake) both modes pass 5/5 by construction. Run: uv run forge eval --suite large --compare solo,team
- S40: S40: --no-defaults only takes effect with --json; plain 'forge run' asks on stdin (EOF = defaults).
- S44: S44: live eval runs (forge eval --models ... --report evals/RESULTS.md) and SWE-bench Lite need API keys and the dataset; only the offline row exists. The SWE-bench runner uses the current Python environment instead of the official per-repo Docker images.
- S45: S45: the full suite takes ~50s on Linux but ~4 min on Windows runners (process start-up); the 60s target holds on Linux/macOS only.
