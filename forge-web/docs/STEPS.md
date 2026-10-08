# Forge Web — step-by-step build plan

Forge Web is built in 17 step cards in two phases. Phase A gets a runnable product early (local isolation,
fake model); phase B makes it complete and safe for many untrusted users. Every card lists files, what to
build, the tests to write first and a verify command. The gate from `AGENTS.md` must pass after every card.

## Phase A · Runnable early

- [x] **W01 — Skeleton**
    - Files: `pyproject.toml` (workspace), `packages/*/pyproject.toml`, `forge_sandbox/{__init__,__main__,cli}.py`, `forge_web/{__init__,__main__,cli,app,settings}.py`, `tests/test_smoke.py`, `tests/test_settings.py`, `.github/workflows/forge-web.yml`, `AGENTS.md`, `PROGRESS.md`, `README.md`, `docs/STEPS.md`
    - Build: uv workspace with Forge as a path dependency; `forge-web --version`, `forge-web serve`; settings from defaults → `forge-web.toml` → `FORGE_WEB_<SECTION>__<KEY>`; data folder per OS; `GET /api/health`; CI on ubuntu, macOS and Windows.
    - Tests: both packages import; both commands print their version; layering; unknown key names the file; health answers.
    - Verify: `uv run forge-web --version && uv run pytest -q`
- [x] **W02 — Wire protocol**
    - Files: `forge_sandbox/frames.py`, `forge_sandbox/protocol.py`, `forge_sandbox/mux.py`, `docs/PROTOCOL.md`, `tests/test_frames.py`, `tests/test_mux.py`
    - Build: length-prefixed binary frames (u32 length, u32 channel, u8 type, payload ≤ 64 KiB); JSON control messages as pydantic models with a discriminator; a multiplexer over one byte stream with per-channel credit windows (256 KiB), control frames first, version handshake.
    - Tests: round trip of every frame type and control message; a channel without credit does not delay control frames or other channels; an oversized or malformed frame closes the connection with a clear error; version mismatch is reported.
    - Verify: `uv run pytest tests/test_frames.py tests/test_mux.py -q`
- [x] **W03 — Sandbox daemon (local)**
    - Files: `forge_sandbox/daemon.py`, `forge_sandbox/attach.py`, `forge_sandbox/fsops.py`, `forge_sandbox/procs.py`, `forge_sandbox/pty.py`, `forge_sandbox/forward.py`, `forge_sandbox/gitinfo.py`, `forge_web/containers/{driver,local}.py`, `forge_web/sandbox_client.py`, `tests/test_fsops.py`, `tests/test_daemon.py`
    - Build: `forge-sandbox daemon` listening on a unix socket (TCP loopback + secret on Windows); `forge-sandbox attach` relays stdio to it; file operations confined to the workspace (no symlink escape, no device files); processes with logs; PTY sessions; tcp-forward both ways; listening ports; git status and diff; `LocalDriver` and `SandboxClient` on the server.
    - Tests: symlink and `..` escapes refused; write/read/rename/delete round trip; process start/output/stop; PTY echo (POSIX); TCP echo through a forward; git status of a changed file.
    - Verify: `uv run pytest tests/test_fsops.py tests/test_daemon.py -q`
- [x] **W04 — Forge worker and `forge-web dev-chat`**
    - Files: `forge_sandbox/worker.py`, `forge_sandbox/pipe_renderer.py`, `forge_sandbox/history.py`, `forge_sandbox/prompts.py`, `forge_sandbox/chats.py`, `forge_web/dev_chat.py`, `tests/test_worker.py`, `tests/fixtures/fake/*.json`
    - Build: one worker process per chat started by the daemon; `PipeRenderer` turns ask/approve into request messages and waits for answers; follow-up prompts carry a "conversation so far" block; slash commands via Forge's `handle_command`; cancel; per-chat event buffer with `seq` and `resume(after_seq)`; chat options (model, mode) as config overrides; a terminal client for trying it out.
    - Tests: a FakeProvider script yields the expected events; an approval round trip; a question round trip; cancel during a tool call; worker killed → the chat resumes from Forge's store; replay after a reconnect has no gaps.
    - Verify: `uv run pytest tests/test_worker.py -q && uv run forge-web dev-chat --fake --prompt "hi"`
- [ ] **W05 — Single-user server and frontend scaffold**
    - Files: `forge_web/db/{engine,models,writer}.py`, `forge_web/db/migrations/`, `forge_web/chats/{api,runs,events}.py`, `forge_web/ws.py`, `forge_web/projects.py`, `forge_web/static.py`, `frontend/` (Vite, React, TypeScript, Tailwind), `tests/test_runs.py`, `tests/test_ws.py`
    - Build: async SQLAlchemy with Alembic migrations, a single writer task (batched commits, WAL); projects (empty only) and chats; `RunManager` keeps one sandbox connection per project and forwards chat traffic; event log (deltas merged, tool output capped); pending requests with "first answer wins"; `/api/ws` with `subscribe(chat_id, after_seq)`; a dev login token; the React app with a sidebar of projects and chats and a basic chat view, served as static files.
    - Tests: replay after `after_seq` has no gaps or duplicates; two tabs answer the same approval → the first wins; a run survives a browser reconnect; static index served with an SPA fallback.
    - Verify: `uv run pytest -q && (cd frontend && npm run build)` then `uv run forge-web serve --dev --fake` and chat in the browser.

## Phase B · Complete and safe

- [ ] **W06 — Docker isolation**
    - Files: `docker/sandbox.Dockerfile`, `forge_web/containers/docker.py`, `forge_web/containers/lifecycle.py`, `forge_web/settings.py`, `tests/test_docker_driver.py`, `tests/docker/`
    - Build: Docker/Podman driver via the CLI; one container per project with the daemon as PID 1 (`--init`), volumes for `/workspace` and `FORGE_HOME`, non-root, `--cap-drop ALL`, `no-new-privileges`, read-only root with tmpfs, CPU/memory/PIDs/nofile limits, `--network none`, gVisor when available; start on demand, stop when idle (LRU); protocol version check recreates outdated containers; `forge-web sandbox build`.
    - Tests: the generated `docker run` arguments (offline); with Docker: daemon restart of the server mid-run keeps the run and re-sends the open approval; limits visible in `docker inspect`; host and metadata address unreachable.
    - Verify: `uv run pytest tests/test_docker_driver.py -q` (and `-m docker` in CI)
- [ ] **W07 — LLM gateway and egress proxy**
    - Files: `forge_web/gateway/{tokens,upstreams,meter,proxy}.py`, `forge_web/egress.py`, `forge_web/keys.py`, `tests/test_gateway.py`, `tests/test_egress.py`
    - Build: run tokens bound to user, chat and run; upstreams from Forge's provider presets (Anthropic, OpenAI-compatible incl. Ollama, Gemini); path and model allowlists; the right auth header per provider; cost reserved before and settled from the upstream's usage; monthly limits per user; user keys (BYOK) and server keys granted to users, encrypted with MultiFernet (master key outside the database); a CONNECT proxy with a domain allowlist and no private addresses, reached from containers through tcp-forward.
    - Tests: no real key reaches the container; over the limit → blocked; aborted stream still charged; wrong path or model → 403; token dead after the run; egress to a private IP or an unlisted domain refused.
    - Verify: `uv run pytest tests/test_gateway.py tests/test_egress.py -q`
- [ ] **W08 — Accounts, sessions and access control**
    - Files: `forge_web/auth/{passwords,sessions,csrf,setup,signup,mail,ratelimit}.py`, `forge_web/access.py`, `forge_web/audit.py`, `tests/test_auth.py`, `tests/test_access.py`
    - Build: argon2id passwords (bounded parallel hashing); server-side sessions in `__Host-` cookies with revocation; CSRF header on every unsafe request; Origin check on WebSocket connect; first admin via a one-time setup token or `forge-web user add --admin`; sign-up modes invite / approval / open (+ allowed domains); email verification and reset links (SMTP optional, else admin-made links); project members with roles owner / editor / viewer; audit log.
    - Tests: every endpoint × non-member → denied (a matrix test over the route table); missing CSRF → 403; wrong Origin rejected; login rate limit; each sign-up mode enforced.
    - Verify: `uv run pytest tests/test_auth.py tests/test_access.py -q`
- [ ] **W09 — Google and GitHub sign-in**
    - Files: `forge_web/auth/oauth.py`, `forge_web/auth/github_repos.py`, `tests/test_oauth.py`
    - Build: Authlib with PKCE, state and nonce; Google OIDC (`email_verified` required); GitHub OAuth with the verified primary email; link accounts only by verified email; "connect GitHub for repositories" as a separate grant (or a personal token), stored encrypted.
    - Tests (mocked identity providers): sign-in creates or links the right user; an unverified email is not linked; state or nonce mismatch rejected.
    - Verify: `uv run pytest tests/test_oauth.py -q`
- [ ] **W10 — Complete chat UI**
    - Files: `frontend/src/**`, `frontend/src/**/*.test.tsx`, `frontend/src/fixtures/*.json`
    - Build: sidebar (projects → chats, search, status), transcript with streaming Markdown (no raw HTML), tool cards (diffs for edits, live shell output, sub-agents, todos), plan card, approval and question cards, report card with cost, composer with slash and @file completion, model and mode pickers, German and English, light and dark, mobile layout.
    - Tests (Vitest): recorded event fixtures render the expected cards; an XSS fixture renders inert; approval buttons send the right answer.
    - Verify: `cd frontend && npm run typecheck && npm test && npm run build`
- [ ] **W11 — Files and git**
    - Files: `forge_web/files_api.py`, `forge_web/git_api.py`, `forge_web/gitjob.py`, `frontend/src/panels/{Files,Changes}.tsx`, `tests/test_files_api.py`, `tests/test_git_api.py`
    - Build: tree, read, write, search, upload, download through the daemon; status, diff, stage, commit, branches; push, pull and clone in a throwaway git container with a credential helper on stdin and hooks disabled; the Files panel (CodeMirror) and the Changes panel.
    - Tests: no token in `.git/config` or in the project container; a malicious hook never runs; viewers cannot write.
    - Verify: `uv run pytest tests/test_files_api.py tests/test_git_api.py -q`
- [ ] **W12 — Project sources**
    - Files: `forge_web/projects.py`, `forge_sandbox/unzip.py`, `tests/test_projects.py`, `tests/test_unzip.py`
    - Build: empty project, git URL (public or with the user's token), ZIP upload unpacked inside the container, server folder (admins only, under allowed roots); quotas (projects per user, disk); delete with container and volumes.
    - Tests: zip-slip, symlink entries, device entries and zip bombs rejected; a server folder outside the allowlist rejected; quota enforced.
    - Verify: `uv run pytest tests/test_projects.py tests/test_unzip.py -q`
- [ ] **W13 — Terminal**
    - Files: `forge_web/terminals.py`, `frontend/src/panels/Terminal.tsx`, `tests/test_terminals.py`
    - Build: PTY sessions through the daemon, one WebSocket per terminal, xterm.js with tabs and resize.
    - Tests: echo and resize; an output flood does not stall chat events on the same connection.
    - Verify: `uv run pytest tests/test_terminals.py -q`
- [ ] **W14 — Live preview**
    - Files: `forge_web/preview.py`, `forge_web/preview_proxy.py`, `frontend/src/panels/Preview.tsx`, `tests/test_preview.py`
    - Build: suggested dev commands (package.json scripts, Python servers), start/stop, detected ports; an HTTP + WebSocket proxy on `<project>.<preview domain>` (a separate registrable domain with wildcard DNS) with one-time tokens exchanged for a subdomain cookie, app cookies never forwarded, Host and Origin rewritten to `localhost:<port>`; a single-port mode for local single-user installs only.
    - Tests: preview of project A cannot read B (different origin, cookie scoped); the app session cookie is never forwarded; a WebSocket upgrade passes through.
    - Verify: `uv run pytest tests/test_preview.py -q`
- [ ] **W15 — Settings and administration**
    - Files: `forge_web/settings_api.py`, `forge_web/admin_api.py`, `forge_web/auth/totp.py`, `frontend/src/pages/{Settings,Admin}.tsx`, `tests/test_admin.py`, `tests/test_totp.py`
    - Build: profile, linked sign-ins, API keys, TOTP two-factor (required for admins if set), default models; admin: users, invites, roles, quotas, server keys and grants, usage, audit log, sign-up mode, sandbox limits.
    - Tests: usage totals equal the database; TOTP codes (RFC 6238 vectors); admin-only routes refuse members.
    - Verify: `uv run pytest tests/test_admin.py tests/test_totp.py -q`
- [ ] **W16 — Deployment**
    - Files: `docker/server.Dockerfile`, `compose.yaml`, `deploy/{Caddyfile,forge-web.service,com.forge.web.plist,forge-web-winsw.xml}`, `forge_web/doctor.py`, `.github/workflows/forge-web-release.yml`, `docs/EINRICHTUNG.md`, `README.md`
    - Build: server image and compose file (server + Caddy with automatic HTTPS); service templates for Linux, macOS and Windows; `forge-web doctor`; a release workflow building the wheel with the web UI and the images; a German step-by-step setup guide including Google and GitHub OAuth apps.
    - Tests: `doctor` reports a missing Docker or image; the wheel contains the built UI.
    - Verify: `uv run pytest tests/test_doctor.py -q`
- [ ] **W17 — End-to-end tests and security review**
    - Files: `tests/e2e/`, `docs/SECURITY.md`
    - Build: Playwright flows — sign in, create a project, chat with the fake model, approve an edit, see the diff, use the terminal, open the preview — in local mode and (CI) Docker mode; threat model; fixes for what the review finds.
    - Tests: all end-to-end flows pass.
    - Verify: `uv run pytest -q -m e2e`
