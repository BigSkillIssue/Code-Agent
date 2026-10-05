# Progress

Next step: S08

## Done
| Step | Date | Commit | Files | Notes |
| --- | --- | --- | --- | --- |
| S01 | 2026-10-05 | e06bc18 | pyproject.toml, src/forge/__init__.py, src/forge/__main__.py, src/forge/cli.py, tests/test_smoke.py, .github/workflows/ci.yml, PROGRESS.md, .gitignore, .python-version | `forge --version` prints `forge 0.1.0`; CI matrix ubuntu/macos/windows runs the gate |
| S02 | 2026-10-05 | 25b36b7 | src/forge/config.py, src/forge/toml_writer.py, forge.example.toml, tests/test_config.py, src/forge/cli.py | layering, env overrides, trust gate, profiles, `forge config check` with masked secrets |
| S03 | 2026-10-05 | 96a1af6 | src/forge/providers/__init__.py, src/forge/providers/base.py, src/forge/events.py, src/forge/plan.py, tests/test_messages.py | all contract message/event models; `parse_event` reads a JSON line back via a discriminated union on `kind` |
| S04 | 2026-10-05 | 6c6080c | src/forge/ports.py, src/forge/ctx.py, src/forge/local/__init__.py, src/forge/local/memory_bus.py, src/forge/local/memory_store.py, tests/test_ports.py, tests/test_architecture.py | four protocols + data models; MemoryBus (queue per subscriber, `*` = all sessions); MemoryStore with word search; ast-based import rules |
| S05 | 2026-10-05 | 2f7af39 | src/forge/providers/base.py, src/forge/providers/openai_compat.py, src/forge/providers/registry.py, src/forge/providers/fake.py, src/forge/providers/catalog.py, src/forge/providers/sse.py, src/forge/providers/retry.py, src/forge/providers/tokens.py, tests/test_openai_compat.py, tests/live/test_live_openai.py | chat-completions streaming with tool calls, retries 1/2/4/8/16 s, error mapping, usage + cost; FakeProvider with per-role queues; registry with cached instances |
| S06 | 2026-10-05 | 84d0caf | src/forge/tools.py, src/forge/runtime/__init__.py, src/forge/runtime/errors.py, src/forge/runtime/permissions.py, src/forge/hooks.py, src/forge/ctx.py, tests/test_tool_framework.py, tests/fixtures/schemas/dummy.json, tests/support.py, tests/conftest.py | @tool + make_tool_def, schema from signature/Annotated/docstring (refs inlined, titles dropped), call_tool pipeline with approval, hooks, spill to .forge/out/, audit log |
| S07 | 2026-10-05 | (next) | src/forge/tools.py, src/forge/runtime/files.py, src/forge/runtime/ledger.py, src/forge/runtime/ignore.py, src/forge/runtime/search.py, src/forge/runtime/edit.py, src/forge/runtime/readers.py, src/forge/runtime/tree.py, src/forge/runtime/proc.py, src/forge/ctx.py, tests/test_tools_files.py, tests/support.py | read_file (text, images, PDF, notebooks), write_file, edit_file, list_dir, glob, grep (rg + identical Python fallback); read ledger; atomic writes with an undo journal |

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

## Open issues
- S05: `Provider.stream` is declared `def stream(...) -> AsyncIterator[StreamItem]` in the Protocol instead of `async def`: implementations are async generators, and mypy only matches those against a plain `def` returning an iterator. Callers use it exactly as the contract shows (`async for item in provider.stream(req)`).
- S07: TOOLS.md asks read_file to downscale images to 1568 px; no image library is in the dependency list, so images are sent as they are (dimensions read from the file header) and refused above 5 MB. Adding Pillow would allow downscaling.
- S07: TOOLS.md says reads outside the project need approval; that is a permission rule and is implemented with the full permission engine in S28.
