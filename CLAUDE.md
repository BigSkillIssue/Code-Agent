# AGENTS.md — instructions for the coding agent building Forge

## Mission

You are building **Forge**, a provider-agnostic coding agent in Python, one step at a time. You work through `docs/STEPS.md` in order. Each session you complete exactly **one** step, prove it with its verify command, record it in `PROGRESS.md`, commit, and stop.

## Read before every session, in this order

1. `AGENTS.md` (this file) — rules that always apply.
2. `PROGRESS.md` — which step is next and any notes from earlier steps.
3. The next step's card in `docs/STEPS.md` — your task, files, tests and verify command.
4. The sections of `docs/CONTRACTS.md` that the step card names — binding signatures.
5. `docs/TOOLS.md` only if the step builds a tool; `docs/PLAN.md` only when the card refers to it.

Do not read the whole of `docs/` every session; read what the step card points to.

## Priority when documents disagree

User instruction in the session > `AGENTS.md` > `docs/CONTRACTS.md` > step card in `docs/STEPS.md` > `docs/TOOLS.md` > `docs/PLAN.md`. If two documents conflict, follow the higher one and write the conflict into `PROGRESS.md` under *Open issues*.

## Hard rules (never break these)

1. **All tools live in `src/forge/tools.py`.** Never define a tool anywhere else. Helpers a tool needs go in `runtime/`.
2. **All model instructions live in `src/forge/prompts.py`.** No prompt text in any other file, not even one sentence.
3. **No server code.** Do not create `server/`, FastAPI apps, HTTP endpoints, Redis or Postgres code. Server support exists only as the interfaces in `ports.py`.
4. **The core talks only to ports.** `pipeline.py`, `agent.py`, `team.py`, `compress.py` and `tools.py` must not import `textual`, `rich`, `sqlite3`, `sqlalchemy`, `subprocess` or anything in `forge.local`. They receive implementations via `Ctx`.
5. **Dependencies point inward:** `cli`/`tui`/`api` → `pipeline` → `agent` → `providers` + `tools` → `runtime` + `ports`. Never import upward.
6. **Contracts are fixed.** Implement the signatures in `docs/CONTRACTS.md` exactly. If a contract seems wrong, do not change it silently: implement it as written and add an *Open issue* in `PROGRESS.md`.
7. **One step per session.** Do not start the next step, and do not build features from later steps "while you are there".
8. **No file over 500 lines,** except `tools.py` and `prompts.py`. Split by responsibility, not by size.
9. **Only the dependencies listed below.** Adding any other package requires asking first.
10. **Never call real LLM APIs in unit tests.** Use `FakeProvider` and recorded fixtures; live tests are marked `@pytest.mark.live` and skipped by default.
11. **Never weaken a test to make it pass.** If a test from an earlier step fails, fix the code or report it.

## Forge Web (`forge-web/`)

`forge-web/` is a separate product: a multi-user server with a web UI built on top of Forge, added at the
user's request. Hard rule 3 ("no server code") does not apply inside `forge-web/`; everywhere else it still
does. Forge never imports Forge Web, and Forge Web never changes anything under `src/` or `tests/`. Work on
Forge Web follows `forge-web/AGENTS.md`, `forge-web/PROGRESS.md` and `forge-web/docs/STEPS.md`.

## Stack and allowed dependencies

Python **3.12+**, managed with **uv**. Use only these packages; add each one in the step that first needs it, not earlier.

| Package | Used for | First step |
| --- | --- | --- |
| `pydantic` (v2) | all models, config, tool args | S02 |
| `httpx` | HTTP for providers and `web_fetch` | S05 |
| `openai` | OpenAI-compatible chat and responses APIs | S05 |
| `rich` | plain CLI renderer | S10 |
| `sqlalchemy` (v2) + `aiosqlite` | `SqliteStore` | S19 |
| `anthropic` | Anthropic, Bedrock, Vertex adapter | S22 |
| `google-genai` | Gemini adapter | S23 |
| `litellm` | catch-all provider | S25 |
| `tree-sitter` + `tree-sitter-language-pack` | `repo_map` | S26 |
| `markdownify` | HTML → Markdown in `web_fetch` | S26 |
| `textual` | terminal UI | S29 |
| `mcp` | MCP client | S36 |
| `playwright` | browser agent (Chromium via `forge browser install`) | S50 |
| dev: `pytest`, `pytest-asyncio`, `respx`, `ruff`, `mypy` | tests, HTTP mocks, lint, types | S01 |

Standard library first: `tomllib` for config, `asyncio` for processes and concurrency, `logging` for logs, `pathlib` for paths. External binaries: `git` (required), `rg` (optional; `grep` falls back to Python).

## Commands

Run these exact commands; every step's verify command builds on them.

```bash
uv sync                                # install deps
uv run ruff format .                   # format
uv run ruff check . --fix              # lint
uv run mypy src                        # types (strict mode, see pyproject)
uv run pytest -q                       # all offline tests
uv run pytest -q -m live               # live provider tests (needs API keys)
uv run forge --help                    # the CLI itself
```

**The gate for every step:** `uv run ruff check . && uv run mypy src && uv run pytest -q` must pass with zero errors, plus the step's own verify command.

## Workflow for every step

1. **Orient.** Read `PROGRESS.md`, find the first step not marked done, read its card. Run the gate command once to confirm the repo is green before you change anything. If it is red, stop and report.
2. **Plan.** Write a short plan (5–10 lines) in your reply: files to create or change, functions to add, tests to write. Stay inside the card's *Files* list; if you need another file, say why.
3. **Tests first.** Write the tests listed under *Tests* in the card. Run them and confirm they fail for the right reason.
4. **Implement.** Write the smallest code that makes the tests pass and matches `docs/CONTRACTS.md`.
5. **Verify.** Run the gate command and the card's *Verify* command. Fix until both pass. Never skip, xfail or delete a test to get green.
6. **Record.** Update `PROGRESS.md`: mark the step done, list files changed, note decisions and open issues.
7. **Commit.** One commit per step: `S07: core file tools (read_file, write_file, edit_file, list_dir, grep, glob)`.
8. **Stop.** Report in 5 lines or fewer: what was built, verify output (pass counts), open issues, next step id. Do not continue to the next step unless the user says so.

If a step is too big for one session, split it into `S07a`, `S07b` in `PROGRESS.md`, finish `S07a` completely, and stop.

## Code style

The code must be easy for a new human reader to follow; prefer obvious over clever.

- Full type hints; `mypy --strict` clean. Pydantic models for data that crosses a module boundary, plain dataclasses inside a module.
- Every public function and class has a one-line docstring saying what it does, not how.
- Functions under 40 lines; one responsibility per module, named for what it does (`patch.py`, not `utils.py`).
- `async` for all I/O. No blocking calls in async code; run blocking work with `asyncio.to_thread`.
- No global mutable state. Everything a function needs comes in as arguments or via `Ctx`.
- Expected failures (file not found, command failed, model refused) are values (`ToolResult(ok=False, ...)`), not exceptions. Exceptions are for bugs.
- Paths are `pathlib.Path`, always resolved against the project root. Text files are read and written as UTF-8 and keep their line endings.
- Comments explain *why*, never restate the code. No commented-out code.
- Cross-platform from the first line: no hard-coded `/`, no bash-only assumptions outside `runtime/shell.py`.

## Testing rules

- Test file per module: `tests/test_<module>.py`. Shared fixtures in `tests/conftest.py`: `tmp_project` (a temp git repo), `fake_provider`, `ctx`.
- `FakeProvider` replays a scripted list of model turns (text and tool calls) and records what it received, so tests can assert on prompts and tool results.
- Recorded fixtures for real providers live in `tests/fixtures/<provider>/*.json`; re-record only with `pytest -m live --record`.
- Tests that need a specific OS use `@pytest.mark.skipif` with a reason; every feature has at least one test that runs on all three OSes.
- Offline test suite stays under 60 seconds.

## Definition of done (every step)

- [ ] Everything in the card's *Build* list exists and matches `docs/CONTRACTS.md`.
- [ ] Every test in the card's *Tests* list exists and passes.
- [ ] Gate command and *Verify* command pass.
- [ ] No hard rule broken (check the list above once more).
- [ ] `PROGRESS.md` updated and one commit made.

## Stop and ask the user when

- A step needs a package not in the dependency table, or an API key you do not have.
- A contract in `docs/CONTRACTS.md` cannot be implemented as written.
- The gate is red before you start, or a test from an earlier step breaks and the cause is outside the current step.
- The verify command still fails after 3 honest attempts.
- A step would require touching more than 2 files outside its *Files* list.

Ask one concrete question with your proposed answer, e.g. "S22 needs `boto3` for Bedrock auth. Add it, or skip Bedrock until S25 via LiteLLM? I suggest LiteLLM." Do not guess on these; on everything else, decide, and note the decision in `PROGRESS.md`.

## PROGRESS.md format

Create it in S01 and keep exactly this shape:

```markdown
# Progress

Next step: S08

## Done
| Step | Date | Commit | Files | Notes |
| --- | --- | --- | --- | --- |
| S07 | 2026-10-12 | a1b2c3d | tools.py, runtime/ledger.py, tests/test_tools_files.py | edit_file returns 3 lines of context |

## Decisions
- S05: retries use exponential backoff 1s, 2s, 4s, max 5 tries.

## Open issues
- S06: CONTRACTS says ToolResult.text max 30000 chars; spill path not in contract yet.
```

## Kickoff prompt

Paste this into the coding agent (Claude Code, Codex or Forge itself) to start each session:

```text
Read AGENTS.md and PROGRESS.md. Then do the next step from docs/STEPS.md,
following the workflow in AGENTS.md exactly: plan, tests first, implement,
run the gate and the step's verify command, update PROGRESS.md, commit, stop.
Only this one step. Report in 5 lines when done.
```

For the very first session, add: "PROGRESS.md does not exist yet; start with S01."
