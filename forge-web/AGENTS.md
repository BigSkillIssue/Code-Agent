# AGENTS.md — instructions for the coding agent building Forge Web

## Mission

You are building **Forge Web**: a multi-user server with a web UI for the Forge coding agent. People sign in
(Google, GitHub, email + password), keep several projects, chat with Forge in each project, and use a file
tree with an editor, a changes/git panel, a terminal and a live preview, much like the Claude Code desktop app.
Users may be strangers, so every project runs in its own hardened container.

Forge Web is a **separate product** in `forge-web/`. It builds on Forge (`../src/forge`) and never changes it.
Work through `docs/STEPS.md` in order, record each step in `PROGRESS.md`, one commit per step.

## Read before every session

1. This file, then `PROGRESS.md`, then the next card in `docs/STEPS.md`.
2. `docs/ARCHITECTURE.md` and `docs/PROTOCOL.md` when the card touches the sandbox or the wire protocol.
3. `docs/SECURITY.md` when the card touches auth, the gateway, containers, previews or files.

## Hard rules

1. **Never change Forge.** Nothing under `../src/forge` or `../tests` changes for Forge Web. If Forge lacks
   something, build it here on top of Forge's public modules, or write an *Open issue* in `PROGRESS.md`.
   Forge's own changes arrive from `main` through `forge-web-sync` (`scripts/sync-core.sh`); when one breaks
   Forge Web, fix Forge Web, not Forge.
2. **Two packages, one direction.** `forge_sandbox` (runs inside a project's container) never imports
   `forge_web`. `forge_web` (the server) imports only the shared wire modules of the sandbox package —
   `frames`, `protocol`, `methods`, `mux`, `rpc`, `streams`, `fingerprint` — never the daemon, its services or
   the worker.
   Both may import Forge.
3. **The container is hostile.** Every frame, event, path and number that comes out of a container is
   validated and size-limited. The server never trusts usage reported by a worker, never uses a path from a
   container on the host, and never runs git or any project code on the host.
4. **Real secrets never enter a container.** Provider API keys, git tokens, OAuth secrets and the server's
   keys stay on the server. Containers get short-lived run tokens only.
5. **All model instructions live in `forge_sandbox/prompts.py`** (Forge's own prompts stay in Forge).
6. **No file over 500 lines.** Split by responsibility.
7. **Only the dependencies listed below.** Adding any other package requires asking first.
8. **Never call real LLM APIs in tests.** Use Forge's `FakeProvider`; live tests are marked `live`.
9. **Never weaken a test to make it pass.**

## Stack and allowed dependencies

Python **3.12+**, **uv** workspace (`packages/sandbox`, `packages/server`), Forge as an editable path
dependency.

| Package | Used for |
| --- | --- |
| `forge` (path `..`) | the agent, events, ports, config, FakeProvider |
| `pydantic` (v2) | every model that crosses a module boundary |
| `fastapi`, `uvicorn[standard]` | HTTP and WebSocket server |
| `sqlalchemy` (v2) + `aiosqlite`, `alembic`; optional `asyncpg` | server database and migrations |
| `httpx` | gateway upstream calls, OAuth, tests |
| `authlib`, `itsdangerous` | Google OIDC and GitHub OAuth |
| `argon2-cffi` | password hashing |
| `cryptography` | encryption of stored keys and tokens (MultiFernet) |
| `python-multipart` | form and upload parsing |
| optional `pywinpty` | terminal in local isolation mode on a Windows host |
| dev: `pytest`, `pytest-asyncio`, `respx`, `ruff`, `mypy`, `playwright` | tests, mocks, lint, types, e2e |

Frontend (`frontend/`, npm): react, react-dom, react-router-dom, zustand, react-markdown, remark-gfm,
highlight.js, @uiw/react-codemirror and language packs, diff, @xterm/xterm, @xterm/addon-fit, lucide-react,
tailwindcss, vite, typescript, vitest, @testing-library/react, jsdom.

External programs: `git` and `docker` (or `podman`) on the server; inside the sandbox image: Python, git,
node, ripgrep.

## Commands

```bash
cd forge-web
uv sync                                  # install
uv run ruff format . && uv run ruff check . --fix
uv run mypy packages                     # strict
uv run pytest -q                         # offline tests (no docker, no e2e)
uv run pytest -q -m docker               # needs a Docker daemon
uv run pytest -q -m e2e                  # needs the built frontend and Chromium
(cd frontend && npm ci && npm run typecheck && npm test && npm run build)
uv run forge-web serve --dev             # local server
```

**Gate for every step:** `uv run ruff check . && uv run mypy packages && uv run pytest -q`, plus the frontend
checks once `frontend/` exists, plus the step's verify command. Forge's own gate in the repository root
(`uv run ruff check . && uv run mypy src && uv run pytest -q`) must stay green.

## Code style

Same as Forge: obvious over clever, full type hints (`mypy --strict`), one-line docstring on every public
function and class, functions under 40 lines, `async` for all I/O, no global mutable state, expected failures
are values (HTTP errors with a clear message), `pathlib.Path` everywhere, comments explain why.
Cross-platform: the server runs on Linux, macOS and Windows; the sandbox runs in a Linux container (or on the
host in local mode).

## Testing rules

- Test file per module under `tests/`; shared fixtures in `tests/conftest.py`.
- Tests that need Docker are marked `docker`, browser tests `e2e`, Postgres tests `postgres`.
- Security tests are written with the feature, not at the end.
- Offline suite stays under 90 seconds. Every real chat worker costs 2-3 s of Forge imports, so a test starts
  one only when it needs a real process; everything else runs the worker in the test process.

## PROGRESS.md format

Same shape as Forge's: *Next step*, a *Done* table (Step, Date, Commit, Files, Notes), *Decisions*,
*Open issues*.
