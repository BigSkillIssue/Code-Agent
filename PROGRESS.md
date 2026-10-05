# Progress

Next step: S22

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
| S21 | 2026-10-05 | (next) | src/forge/providers/catalog.py, src/forge/providers/registry.py, tests/test_registry.py | 17 provider presets (zero-config for known vendors), model catalog with capabilities and prices, aliases (claude-sonnet → claude-sonnet-5-5), config overrides, fallback chain tested end to end |

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

## Open issues
- S05: `Provider.stream` is declared `def stream(...) -> AsyncIterator[StreamItem]` in the Protocol instead of `async def`: implementations are async generators, and mypy only matches those against a plain `def` returning an iterator. Callers use it exactly as the contract shows (`async for item in provider.stream(req)`).
- S07: TOOLS.md asks read_file to downscale images to 1568 px; no image library is in the dependency list, so images are sent as they are (dimensions read from the file header) and refused above 5 MB. Adding Pillow would allow downscaling.
- S07: TOOLS.md says reads outside the project need approval; that is a permission rule and is implemented with the full permission engine in S28.
- S08: `CommandResult` gained four optional fields that the shell and job tools need and the contract lacks: `cwd` (folder after a shell command), `pid`, `elapsed_s` and `total_lines` (job status and paging). `ports.JobNotFoundError` was added for unknown job ids. All contract fields are unchanged.
- S14: `Ctx` gained one field beyond the contract, `state: SessionState` (default factory): runtime state shared by all agents of a session — the usage tally now, the agent registry and budgets later. All contract fields are unchanged.
