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
- [x] **W05a — Single-user server**
    - Files: `forge_web/db/{engine,models,writer}.py`, `forge_web/db/migrations/`, `forge_web/chats/{api,runs,items}.py`, `forge_web/{hub,ws,projects,access,services,fake,app,cli}.py`, `forge_web/auth/dev.py`, `tests/test_server.py`
    - Build: async SQLAlchemy with Alembic migrations, a single writer task (batched commits, WAL); projects (empty only) and chats; `RunManager` keeps one sandbox connection per project and a relay per chat; items from the sandbox are validated and rebuilt; stored items get gapless numbers, model text and command output stream live only; `/api/ws` with `subscribe(chat_id, after_seq)`; a development login link.
    - Tests: replay after `after_seq` has no gaps or repeats; two tabs answer the same approval → the first wins; a tab that reconnects mid-turn misses nothing; project lifecycle; sign-in required.
    - Verify: `uv run pytest tests/test_server.py -q`
- [x] **W05b — Web UI scaffold**
    - Files: `frontend/` (Vite, React, TypeScript, Tailwind), `forge_web/webui.py`, `tests/test_webui.py`
    - Build: the React app with a sidebar of projects and chats and a basic chat view (streaming text, tool calls, approval and question cards, composer with stop), a WebSocket client that resumes after its last number; built into `forge_web/static` and served with an SPA fallback.
    - Tests: Vitest for the item reducer; the server serves `index.html` for app routes and never for `/api/*`.
    - Verify: `(cd frontend && npm run typecheck && npm test && npm run build) && uv run pytest tests/test_webui.py -q`, then `uv run forge-web serve --dev --fake` and chat in the browser.

## Phase B · Complete and safe

- [x] **W06 — Docker isolation**
    - Files: `docker/sandbox.Dockerfile`, `forge_web/containers/docker.py`, `forge_web/containers/lifecycle.py`, `forge_web/settings.py`, `tests/test_docker_driver.py`, `tests/docker/`
    - Build: Docker/Podman driver via the CLI; one container per project with the daemon as PID 1 (`--init`), volumes for `/workspace` and `FORGE_HOME`, non-root, `--cap-drop ALL`, `no-new-privileges`, read-only root with tmpfs, CPU/memory/PIDs/nofile limits, `--network none`, gVisor when available; start on demand, stop when idle (LRU); protocol version check recreates outdated containers; `forge-web sandbox build`.
    - Tests: the generated `docker run` arguments (offline); with Docker: daemon restart of the server mid-run keeps the run and re-sends the open approval; limits visible in `docker inspect`; host and metadata address unreachable.
    - Verify: `uv run pytest tests/test_docker_driver.py -q` (and `-m docker` in CI)
- [x] **W07 — LLM gateway and egress proxy**
    - Files: `forge_web/gateway/{tokens,upstreams,meter,proxy}.py`, `forge_web/egress.py`, `forge_web/keys.py`, `tests/test_gateway.py`, `tests/test_egress.py`
    - Build: run tokens bound to user, chat and run; upstreams from Forge's provider presets (Anthropic, OpenAI-compatible incl. Ollama, Gemini); path and model allowlists; the right auth header per provider; cost reserved before and settled from the upstream's usage; monthly limits per user; user keys (BYOK) and server keys granted to users, encrypted with MultiFernet (master key outside the database); a CONNECT proxy with a domain allowlist and no private addresses, reached from containers through tcp-forward.
    - Tests: no real key reaches the container; over the limit → blocked; aborted stream still charged; wrong path or model → 403; token dead after the run; egress to a private IP or an unlisted domain refused.
    - Verify: `uv run pytest tests/test_gateway.py tests/test_egress.py -q`
- [x] **W08a — Accounts and sessions**
    - Files: `forge_web/auth/{passwords,sessions,onetime,origin,ratelimit,mail,routes,dev}.py`, `forge_web/{audit,user_cli,startup,services,settings,app}.py`, `forge_web/db/migrations/versions/0003_accounts.py`, `frontend/src/pages/Auth.tsx`, `frontend/src/api/client.ts`, `tests/test_auth.py`, `tests/test_user_cli.py`
    - Build: argon2id passwords (bounded parallel hashing); server-side sessions (only the token's hash is stored) in HttpOnly cookies (`__Host-` over HTTPS); a CSRF value every changing request must send; Origin checks for API calls and WebSockets; the first admin via a one-time setup link; sign-up modes invite / approval / open (+ allowed domains); invites, reset and email links that work once; SMTP optional; rate limits; audit log; `forge-web user add|reset-link|list`; sign-in, setup, sign-up, reset and verify pages.
    - Tests: setup token; sign-in, sign-out, rate limit; missing or wrong CSRF → 403; cross-site requests and WebSockets refused; each sign-up mode; a wrong email cannot burn an invite; a reset link works once and ends other sessions.
    - Verify: `uv run pytest tests/test_auth.py tests/test_user_cli.py -q`
- [x] **W08b — Members, administration and the access matrix**
    - Files: `forge_web/members.py`, `forge_web/auth/admin.py`, `tests/test_access.py`
    - Build: project members with roles owner / editor / viewer; admin API for users (approve, roles, disable), invites and reset links; a user's own sessions (list, end).
    - Tests: every route × an outsider is refused (a matrix over the route table); viewers cannot change anything; admin routes refuse members.
    - Verify: `uv run pytest tests/test_access.py -q`
- [x] **W09 — Google and GitHub sign-in**
    - Files: `forge_web/auth/oauth.py`, `forge_web/auth/oauth_providers.py`, `forge_web/auth/git_credentials.py`, `tests/test_oauth.py`
    - Build: Authlib with PKCE, state and nonce; Google OIDC (`email_verified` required); GitHub OAuth with the verified primary email; link accounts only by verified email; "connect GitHub for repositories" as a separate grant (or a personal token), stored encrypted.
    - Tests (mocked identity providers): sign-in creates or links the right user; an unverified email is not linked; state or nonce mismatch rejected.
    - Verify: `uv run pytest tests/test_oauth.py -q`
- [x] **W10a — What the chat UI needs from the server and the sandbox**
    - Files: `forge_sandbox/{gitinfo,methods,worker}.py`, `forge_web/{files_api,app}.py`, `forge_web/gateway/api.py`, `forge_web/chats/items.py`, `tests/{test_daemon,test_worker,test_items,test_models,test_access}.py`
    - Build: `git.files` (files not ignored by git, fuzzy search) and `GET /api/projects/{id}/files/search` for @-mentions; the worker's `ready` item lists the slash commands (Forge's and custom ones), checked by the server; `GET /api/models` (catalog models of the providers a user may use).
    - Verify: `uv run pytest tests/test_models.py tests/test_items.py -q`
- [x] **W10b — Complete chat UI**
    - Files: `frontend/src/**`, `frontend/src/**/*.test.tsx`, `frontend/src/fixtures/*.json`, `tests/record_ui_fixtures.py`
    - Build: sidebar (projects → chats, search, status), transcript with streaming Markdown (no raw HTML), tool cards (diffs for edits, live shell output, sub-agents, todos), plan card, approval and question cards, report card with cost, composer with slash and @file completion, model and mode pickers, German and English, light and dark, mobile layout.
    - Tests (Vitest): recorded event fixtures render the expected cards; an XSS fixture renders inert; approval buttons send the right answer.
    - Verify: `cd frontend && npm run typecheck && npm test && npm run build`
- [x] **W11a — Files and local git actions**
    - Files: `forge_sandbox/{fsops,gitops,methods,daemon}.py`, `forge_web/{files_api,git_api,sandbox_calls,settings,app}.py`, `tests/{test_fsops,test_files_api,test_git_api,test_access}.py`
    - Build: list, read, save, mkdir, rename, delete; uploads and downloads of any size in parts; status, diff, stage, unstage, discard, commit (as the signed-in user), branches, log, remote (https only, no credentials).
    - Tests: viewers cannot change files or the repository; links are not followed; downloads are attachments; a malicious hook never runs on a commit.
    - Verify: `uv run pytest tests/test_files_api.py tests/test_git_api.py -q`
- [x] **W11b — Push and pull in a git job**
    - Files: `forge_web/containers/{gitjob,driver,docker,local}.py`, `forge_web/{gitsync,git_api,settings,services}.py`, `forge_sandbox/{gitops,methods}.py`, `tests/{test_gitsync,test_docker_driver,test_git_api,test_access}.py`
    - Build: bundles in `.git/forge-transfer/` between the project and a git job (throwaway hardened container with the workspace volume; child process in local mode); token on stdin into a credential file in the job's temporary folder; remote https on an allowed host with public addresses, pinned with `--add-host`; push and pull routes; fast-forward only.
    - Tests: no token in the project after a job; hooks, fsmonitor and credential helpers of the project never run; private, non-https and not-allowed remotes refused; the job container is hardened.
    - Verify: `uv run pytest tests/test_gitsync.py -q` (+ `-m docker tests/test_docker_driver.py`)
- [x] **W11c — Files and changes panels**
    - Files: `frontend/src/panels/{ProjectPanel,FileTree,FileEditor,CodeEditor,Changes}.tsx`, `frontend/src/panels/panels.test.tsx`, `frontend/src/api/{client,project}.ts`, `frontend/src/components/ChatView.tsx`, `frontend/src/lib/i18n.ts`, `forge_sandbox/gitinfo.py`, `tests/test_git_api.py`
    - Build: a panel beside the chat (full screen on phones) with the file tree, a CodeMirror editor (loaded on demand, Ctrl/Cmd+S, conflict warning) and the changes view (stage, unstage, discard, diff, commit, branches, remote, push, pull, history); it refreshes when a turn ends. Forge's working files are excluded from git.
    - Verify: `cd frontend && npm run typecheck && npm test && npm run build`; checked in Chromium.
    - Files: `forge_web/files_api.py`, `forge_web/git_api.py`, `forge_web/gitjob.py`, `frontend/src/panels/{Files,Changes}.tsx`, `tests/test_files_api.py`, `tests/test_git_api.py`
    - Build: tree, read, write, search, upload, download through the daemon; status, diff, stage, commit, branches; push, pull and clone in a throwaway git container with a credential helper on stdin and hooks disabled; the Files panel (CodeMirror) and the Changes panel.
    - Tests: no token in `.git/config` or in the project container; a malicious hook never runs; viewers cannot write.
    - Verify: `uv run pytest tests/test_files_api.py tests/test_git_api.py -q`
- [x] **W12a — Project sources and quotas (server and sandbox)**
    - Files: `forge_sandbox/{unzip,usage,fsops,gitops,methods,daemon}.py`, `forge_web/{projects,sources,quotas,gitsync,files_api,startup,settings,services,app,sandbox_calls}.py`, `forge_web/containers/{gitjob,driver,docker,local}.py`, `forge_web/chats/api.py`, `tests/{test_unzip,test_projects,test_docker_driver,test_access}.py`
    - Build: projects from a git URL (a clone job, then the bundle checked out in the sandbox), from a ZIP upload (unpacked in the sandbox), or from a server folder (admins, under `sandbox.folder_roots`, bind-mounted in Docker, never deleted); projects per user and disk per project limited.
    - Tests: zip-slip, links, devices, bombs, too many entries and broken archives refused; folders outside the roots or around the data folder refused; quotas enforced; clone in Docker.
    - Verify: `uv run pytest tests/test_projects.py tests/test_unzip.py -q`
- [x] **W12b — The new-project dialog**
    - Files: `frontend/src/components/{NewProjectDialog,NewProjectDialog.test,Sidebar,ChatView}.tsx`, `frontend/src/{api/project,state/store,lib/i18n}.ts`
    - Build: name and source (empty, git URL, ZIP file, server folder for admins); a ZIP is uploaded once the project exists and the project is removed again if unpacking fails.
    - Verify: `cd frontend && npm run typecheck && npm test && npm run build`; checked in Chromium.
    - Files: `forge_web/projects.py`, `forge_sandbox/unzip.py`, `tests/test_projects.py`, `tests/test_unzip.py`
    - Build: empty project, git URL (public or with the user's token), ZIP upload unpacked inside the container, server folder (admins only, under allowed roots); quotas (projects per user, disk); delete with container and volumes.
    - Tests: zip-slip, symlink entries, device entries and zip bombs rejected; a server folder outside the allowlist rejected; quota enforced.
    - Verify: `uv run pytest tests/test_projects.py tests/test_unzip.py -q`
- [x] **W13 — Terminal**
    - Files: `forge_web/terminals.py`, `frontend/src/panels/Terminal.tsx`, `tests/test_terminals.py`
    - Build: PTY sessions through the daemon, one WebSocket per terminal, xterm.js with tabs and resize.
    - Tests: echo and resize; an output flood does not stall chat events on the same connection.
    - Verify: `uv run pytest tests/test_terminals.py -q`
- [x] **W14 — Live preview** (split: W14a server, proxy and API; W14b the Preview tab)
    - Files: `forge_web/preview.py`, `forge_web/preview_proxy.py`, `frontend/src/panels/Preview.tsx`, `tests/test_preview.py`
    - Build: suggested dev commands (package.json scripts, Python servers), start/stop, detected ports; an HTTP + WebSocket proxy on `<project>.<preview domain>` (a separate registrable domain with wildcard DNS) with one-time tokens exchanged for a subdomain cookie, app cookies never forwarded, Host and Origin rewritten to `localhost:<port>`; a single-port mode for local single-user installs only.
    - Tests: preview of project A cannot read B (different origin, cookie scoped); the app session cookie is never forwarded; a WebSocket upgrade passes through.
    - Verify: `uv run pytest tests/test_preview.py -q`
- [x] **W15 — Settings and administration** (split: W15a server; W15b the Settings and Admin pages)
    - Files: `forge_web/settings_api.py`, `forge_web/admin_api.py`, `forge_web/auth/totp.py`, `frontend/src/pages/{Settings,Admin}.tsx`, `tests/test_admin.py`, `tests/test_totp.py`
    - Build: profile, linked sign-ins, API keys, TOTP two-factor (required for admins if set), default models; admin: users, invites, roles, quotas, server keys and grants, usage, audit log, sign-up mode, sandbox limits.
    - Tests: usage totals equal the database; TOTP codes (RFC 6238 vectors); admin-only routes refuse members.
    - Verify: `uv run pytest tests/test_admin.py tests/test_totp.py -q`
- [x] **W16 — Deployment** (split: W16a doctor, TLS check, wheel; W16b images, compose, services, release, guide)
    - Files: `docker/server.Dockerfile`, `compose.yaml`, `deploy/{Caddyfile,forge-web.service,com.forge.web.plist,forge-web-winsw.xml}`, `forge_web/doctor.py`, `.github/workflows/forge-web-release.yml`, `docs/EINRICHTUNG.md`, `README.md`
    - Build: server image and compose file (server + Caddy with automatic HTTPS); service templates for Linux, macOS and Windows; `forge-web doctor`; a release workflow building the wheel with the web UI and the images; a German step-by-step setup guide including Google and GitHub OAuth apps.
    - Tests: `doctor` reports a missing Docker or image; the wheel contains the built UI.
    - Verify: `uv run pytest tests/test_doctor.py -q`
- [x] **W17 — End-to-end tests and security review** (split: [x] W17a end-to-end tests; [x] W17b security review and sign-in fixes; [x] W17c fixes for the sandbox review)
    - Files: `tests/e2e/`, `docs/SECURITY.md`
    - Build: Playwright flows — sign in, create a project, chat with the fake model, approve an edit, see the diff, use the terminal, open the preview — in local mode and (CI) Docker mode; threat model; fixes for what the review finds.
    - Tests: all end-to-end flows pass.
    - Verify: `uv run pytest -q -m e2e`
- [x] **W18 — Core changes reach Forge Web; open points kept for later** (asked for after W17)
    - Files: `scripts/sync-core.sh`, `../.github/workflows/forge-web-sync.yml` (also on `main`), `deploy/update.sh`, `forge_sandbox/fingerprint.py`, `forge_sandbox/{cli,daemon}.py`, `forge_web/{containers/docker,chats/runs,doctor}.py`, `docs/{SPAETER,EINRICHTUNG}.md`, `README.md`, `AGENTS.md`
    - Build: after each push to `main` that touches Forge, merge `main` into the Forge Web branch, run Forge Web's checks, push only when green (else an issue); a stopped project container is recreated when its image tag points at a newer image; a Forge fingerprint in the sandbox hello, `forge-sandbox fingerprint` and a doctor check; `update.sh` pulls, rebuilds and restarts a compose server; `SPAETER.md` keeps the open decisions.
    - Tests: the sync script merges/pushes, leaves conflicts, failing checks and local changes alone; fingerprints follow the Python files; doctor warns about another Forge; a rebuilt image reaches a project at its next start (Docker); `update.sh` builds the images named in `.env`; the server imports only shared sandbox modules.
    - Verify: `uv run pytest -q tests/test_sync.py tests/test_fingerprint.py tests/test_deploy.py tests/test_doctor.py`

## Phase C · Apple apps (asked for after W18)

Forge builds native Apple apps (Swift, SwiftUI) for iPhone, iPad, Mac and Apple Watch. What belongs to the agent engine — the `AppleBuilder` port, the `apple_build`/`apple_screenshot` tools, the app template, the independent guideline reviewer and the checkpoints with the user's approval — was built in Forge on `main` (S58–S60) and arrives through the sync. Forge Web adds the Macs, the UI and the way to Apple.

- [x] **W19 — Mac build service** (split: W19a server, W19b sandbox, W19c Mac worker)
    - Files: `packages/macworker/` (`forge_macworker/{wire,job,runners,client,images,cli}.py`), `forge_web/apple/{jobs,workers,sandbox_api,worker_api,admin}.py`, `forge_web/db/{models.py,migrations/versions/0006_apple.py}`, `forge_web/{settings,startup,services,app,admin_api}.py`, `forge_web/gateway/proxy.py`, `forge_sandbox/{apple_remote,methods,worker}.py`, `tests/{test_apple_server,test_apple_remote,test_macworker,test_access,test_smoke}.py`
    - Build: a third package `forge-macworker` for Macs: it connects out to the server (worker token), runs each project's jobs in a macOS VM of its own (Tart, softnet network, at most two VMs), or on the Mac in direct mode; the server queues jobs from the sandboxes (gateway `/apple/build` and `/apple/screenshot` with the run token, the packed project as body), checks who may build and the monthly Mac minutes, keeps archives under their job, and has admin endpoints for Macs, jobs and grants; the sandbox's `RemoteAppleBuilder` packs the project without git data and build output; chats of Apple projects get it with the guideline checks on.
    - Tests: a job goes from sandbox to Mac and back; archives stay on the server under their job; screenshots must be PNGs; a Mac sees only its own running jobs; who may build (off, granted, admins, minutes, no Mac online); bodies are checked; jobs time out; admins add, disable and remove Macs and allow users; Mac routes refuse sessions; unpacking refuses paths and links that leave the project; direct mode end to end; one VM per project, deleted when idle.
    - Verify: `uv run pytest -q tests/test_apple_server.py tests/test_apple_remote.py tests/test_macworker.py`
- [x] **W20 — "Apple app" projects**
    - Files: `frontend/src/components/NewProjectDialog.tsx`, `frontend/src/pages/AdminApple.tsx`, `frontend/src/panels/AppleScreens.tsx`, `forge_web/projects.py`, `forge_web/apple/{template,screens_api}.py`, tests
    - Build: the new-project dialog offers "Apple app" (writes `forge apple new`'s template into the project, kind `apple`); Apple projects' chats check the guidelines; developer.apple.com in the egress list for the reviewer; the Preview tab shows the latest screenshot of each device; the admin page for Macs, jobs, grants and minutes; a separate reviewer model can be chosen.
- [x] **W21 — Guideline reviews and approval**
    - Files: `frontend/src/components/cards/GuidelineCard.tsx`, `frontend/src/pages/AppleReview.tsx`, `forge_web/apple/{approvals,questions}.py`, migration `0007`, tests
    - Build: a card per guideline review (request, plan, product) with every finding; a page "Ready for approval" with the screenshots of every device, the reviews and the build results; "Approve" / "Back to the agent"; only an approval by the user (audit log) opens the App Store step.
- [x] **W22a — App Store Connect key and client**
    - Files: `forge_web/apple/{asc_client,asc_keys}.py`, migration `0008`, `settings.py` (`apple.asc_api_url`), `frontend/src/pages/SettingsAppStore.tsx`, `tests/{asc_standin,test_asc_keys}.py`
    - Build: each user's App Store Connect team key (Key ID, Issuer ID, Team ID, `.p8`) encrypted with the vault, never shown again, never sent to a sandbox or a Mac; a client that signs ES256 JWTs itself and passes Apple's error messages on; a key check; a settings section; a stand-in for Apple's API that checks every token.
- [x] **W22b1 — Signing and exporting on the Mac**
    - Files: `forge_macworker/{wire,export,job,runners,client}.py`, `forge_web/apple/{jobs,worker_api}.py`, `tests/test_mac_export.py`
    - Build: a job kind `export` (only for workers from 0.2.0 on) that signs an archive in a fresh VM of its own (Tart: a one-off VM from the clean image, removed after the job; direct mode on the Mac): the Mac that holds the job fetches the certificates and profiles once (`/api/mac/jobs/{id}/signing`, never part of the offer), imports them into a keychain of its own, installs the profiles, runs `xcodebuild -exportArchive` (App Store Connect, manual signing; Mac: signed installer) and sends the .ipa or .pkg back; keychain, profiles and key files are removed whatever happens. Archive jobs with a build number make the ad-hoc signed release archive (S62).
    - Tests: the export signs with its own keychain and puts the user's search list back; a failed export says why and still cleans up; a Mac export is an installer; a release archive asks for the build number; server → Mac → server with the signing material fetched once; old workers get no exports; signing material is never part of an offer.
    - Verify: `uv run pytest -q tests/test_mac_export.py tests/test_macworker.py tests/test_apple_server.py`
- [x] **W22b2 — Release to TestFlight**
    - Files: `forge_web/apple/{release,release_api,signing,upload,archive_info}.py`, `forge_web/apple/asc_client.py`, `forge_sandbox/{gitops,methods}.py` (`git.archive`), `forge_web/db/{models.py,migrations/versions/0009_apple_releases.py}`, `forge_web/{settings,services,startup,app}.py`, `frontend/src/{pages/AppleRelease.tsx,pages/AppleReview.tsx,api/apple.ts,lib/texts/release.ts}`, `tests/{asc_standin,asc_standin_release,test_apple_signing,test_apple_release,test_access}.py`
    - Build: a stored release per platform from the project's newest approval while the project is still at that commit: the commit packed by the sandbox (`git.archive`) and archived on a Mac with a rising build number (S62); its bundles and version read from the archive; bundle ids registered, certificates (made once per user and team, kept encrypted) and fresh profiles through the Provisioning API; signed in a fresh VM (W22b1); uploaded by the server (Build Upload API, parts, MD5) to Apple's own hosts only; Apple's processing awaited; an internal TestFlight group "Forge" with every build. Releases go on after a restart and start again at the failed step. The page "Release" starts and follows them.
    - Tests: an approved commit goes to TestFlight for iPhone (with Watch) and Mac at once, exactly the commit, one certificate of each kind, no token at upload URLs; a release needs a key, an approval and the approved commit; a failed release starts again where it failed; a release goes on after a restart without uploading twice; archives say which bundles to sign; an export is exactly one product; uploads go only to Apple; certificates are made once, renewed when revoked, and the team's limit is explained; every bundle gets a profile and the .p12 files load.
    - Verify: `uv run pytest -q tests/test_apple_release.py tests/test_apple_signing.py tests/test_access.py`
- [x] **W22c — Store texts in Forge Web**
    - Files: `forge_web/apple/{listing,approvals}.py`, `forge_web/db/{models.py,migrations/versions/0010_apple_listings.py}`, `forge_web/app.py`, `frontend/src/{pages/AppleListing.tsx,pages/appleListing.test.tsx,api/listing.ts,lib/texts/listing.ts,components/cards/GuidelineCard.tsx,App.tsx}`, `tests/{test_apple_listing,test_apple_approvals,test_access}.py`
    - Build: the listing Forge drafted (S61, `.forge/out/apple/listing.json`) shown on the page "Store texts", finished by the user (URLs, texts, categories, age rating; the privacy answers to enter by hand) and saved on the server, every save checked with Forge's own `StoreListing` (Apple's limits, keywords in bytes); the saved listing is what goes to Apple, whatever the project's file says later; Forge's newest draft can be taken again; the reviewer's verdict on the store texts (stage "listing") on the approval page.
    - Tests: Forge's draft is shown, finished and saved, and the saved one counts; Apple's limits hold for every save and for drafts; the listing review is on the approval page; the page shows what Apple still needs, counts keywords in bytes and takes a new draft.
    - Verify: `uv run pytest -q tests/test_apple_listing.py tests/test_apple_approvals.py tests/test_access.py`
- [x] **W22d1 — Store screenshots**
    - Files: `forge_web/apple/store_media.py`, `forge_web/apple/{jobs,upload}.py`, `forge_macworker/{store_shots,job,runners,wire,__init__}.py`, `tests/{asc_standin,asc_standin_store,test_store_media,test_mac_export,test_smoke}.py`
    - Build: Apple's screenshot sizes read at run time from its reference data (`appAssetLibraryRefData`), the largest portrait size for iPhone, iPad and Watch and the largest landscape size for the Mac; screenshot jobs with `fit` (workers from 0.3.0 on) that the Mac makes exactly that size with `sips` (scaled, padded, flattened without transparency); the server checks size and transparency and uploads light and dark pictures into the app's App Asset Library (reserve, parts, commit), waiting for Apple's processing; placements for a version localization.
    - Tests: the sizes come from the reference data (and a missing family is named); a store screenshot is exactly its size and has no transparency; the Mac fits a picture (stand-in `sips`; with the real one on CI's Mac); from the Mac into the asset library, older workers get no such jobs.
    - Verify: `uv run pytest -q tests/test_store_media.py tests/test_mac_export.py tests/test_macworker.py`
- [x] **W22d2 — The submission**
    - Files: `forge_web/apple/{submit,submission,submit_api,release,store_media}.py`, `forge_web/db/{models.py,migrations/versions/0011_apple_submissions.py}`, `forge_web/{settings,services,startup,app}.py`, `frontend/src/{pages/{AppStoreSubmit,appStoreSubmit.test,AppleRelease,appleRelease.test}.tsx,api/submissions.ts,lib/texts/submit.ts,lib/i18n.ts}`, `tests/{asc_standin,asc_standin_submit,test_apple_submission,test_apple_release,test_access}.py`, `docs/{EINRICHTUNG,SECURITY,STEPS}.md`
    - Build: a stored submission per release from TestFlight, on the user's click "Prepare for the App Store" with the contact for App Review: store screenshots (W22d1) of every device taken from the released commit and uploaded; the version in preparation with the release's build, copyright and a release by hand; the saved store texts (no "What's new" for a first version), app information, categories and age rating; content rights, a free price and every territory where none are set; the review contact; the screenshots placed, light first. "Submit to Apple" only with a confirmation naming the version and while the release belongs to the newest approval; App Review followed; "Release on the App Store" for an approved version. Bundle IDs are registered before the app record is looked up, so the user can make it with them.
    - Tests: from TestFlight through App Review to the App Store against the stand-ins (every field checked, nothing at App Review before the confirmation, the wrong version refused); only the newest approval and complete store texts go to Apple; the page prepares with a contact, submits only after confirming, shows Apple's state and releases.
    - Verify: `uv run pytest -q tests/test_apple_submission.py tests/test_apple_release.py tests/test_access.py`
- [x] **W23 — End to end and the guide**
    - Files: `../.github/workflows/forge-web.yml` (macOS job), `tests/test_apple_flow.py`, `docs/EINRICHTUNG.md`, `docs/SECURITY.md`
    - Build: a CI job on a macOS runner builds the template through server, sandbox and a Mac worker in direct mode (GitHub's Macs cannot run VMs); a German guide: renting a Mac, Tart and softnet, the image, the worker as a service, the Apple developer account and the API key.

## Phase D · Hosting (the product factory, asked for after W23)

The paying customer only says *what* they want ("an Instagram", "a Windows program"); Forge builds a complete product (server, database, apps, docs, tests, CI), Forge Web hosts it on the admin's own Linux servers after an independent review and two approvals — the creator's, then the admin's (the admin can spare trusted users their click) — and puts it into the stores. Customers pay the admin (phase E) and the apps can take money from their own users (phase F); Android and then Windows follow (phase G). The whole plan, its six base decisions (assumptions until the user confirms them) and what only the user can supply are in `../docs/PRODUKTFABRIK.md` (German); read it before every card of phases D–G.

What belongs to the agent engine — the app manifest, the full-stack template, `forge app dev|check`, the release reviewer, the checkpoints with "Ready to go live" and the blueprint (S63–S68), later the payments module and the platform templates (S69–S72) — is built in Forge on `main` and arrives through the sync. Phase D starts once S63–S68 are here.

Rules for phases D–G, in addition to this `AGENTS.md`:

- Forge Web never changes `../src`. Core cards (S…) are done on `main`; the sync brings them here.
- Tests never call real services (Stripe, Apple, Google, Microsoft, LLMs): every external API gets a stand-in in `tests/*_standin*.py` that checks the real service's signatures and rules.
- No new Python packages without asking: Stripe, Play and the store APIs go through `httpx` + `cryptography` + `hmac`.
- Migration numbers on the cards assume no other card adds one; use the next free number.
- After every push read CI and fix until it is green; a push cancels running runs, so wait for long Mac jobs first.

Security principles (the apps' code is foreign code):

- Hosted apps never run on the Forge Web server (it holds the Docker socket and the vault key).
- gVisor (`runsc`) is required, with a read-only root, `cap-drop ALL`, user namespaces, limits and one network per app; the firewall (`DOCKER-USER`) allows no private networks, no cloud metadata, no port 25 and no other apps, and rate limits stop scans and mining.
- The host worker never runs an app's `docker-compose.yml` or `Dockerfile`: it gets a fully resolved `DeployPlan`. Builds (with `npm install` scripts) run only in throwaway containers without secrets that reach only package registries.
- Results from the sandbox are not trusted: the host checks the packed commit itself (tests, migrations) before the admin sees "Approve". The LLM reviewer only advises; the hard gates are the fixed checks and the two people. "Trusted" only spares the admin's click.
- Secrets are sealed to each host's key (X25519, `cryptography`) and fetched once per deploy.
- Apps send mail and take payments only through Forge Web's relays (quotas, one token per app).
- Impressum, privacy policy and "report content" are served by the edge (Caddy) at fixed paths; the app cannot remove them.

- [x] **W24 — Full-stack projects**
    - Files: `docker/sandbox.Dockerfile` (+ PostgreSQL 16, Chromium), `forge_web/projects.py`, `forge_web/apps/{__init__,template}.py`, `frontend/src/components/NewProjectDialog.tsx`, `tests/{test_app_projects,test_access}.py`
    - Build: the new-project dialog offers "App (server + web)": it writes `forge app new`'s template into the project, kind `app`; chats of app projects run with `--app` (S67b); the preview shows the web port from `forge.app.toml`; the sandbox image gets Postgres and Chromium so `forge app dev`, `forge app check` and the product screenshots work inside it.
    - Tests: an app project gets the template and kind `app`; its chats run with the app checkpoints; the preview port comes from the manifest; under the `docker` marker `forge app check` passes inside the image on a freshly made product.
    - Verify: `uv run pytest -q tests/test_app_projects.py tests/test_access.py`; with Docker: `uv run pytest -q -m docker tests/test_app_projects.py`
- [x] **W25a — Host worker: run a release**
    - Files: `packages/hostworker/pyproject.toml`, `forge_hostworker/{__init__,wire,client,build,runner,postgres,cli}.py`, `pyproject.toml` (workspace), `AGENTS.md` (package rule), `tests/{test_hostworker,test_smoke}.py`
    - Build: a new package `forge-host-worker`, a copy of the Mac-worker pattern: it connects out to the server with a host token (long polling) and receives a `DeployPlan` (fixed runtime images only, limits, names of secrets); builds in throwaway `runsc` containers; runs apps only with `runsc`; one Postgres container and one network per app; health checks; keeps the previous release for rollback; refuses to start without `runsc`. `AGENTS.md` rule 2 gains the package: it imports neither `forge_sandbox` nor `forge_web`, and the server imports only its `wire`.
    - Tests (a fake `docker` binary records every call): a plan builds and runs with every hardening flag; an app's Dockerfile or compose file is never used; no secret reaches a build container; a failed health check switches back to the previous release; one network and one Postgres per app; without `runsc` it refuses to run; the import rule holds.
    - Verify: `uv run pytest -q tests/test_hostworker.py tests/test_smoke.py`
- [x] **W25b — Host edge and firewall**
    - Files: `forge_hostworker/{edge,firewall,doctor}.py`, `deploy/host/*`, `docs/EINRICHTUNG.md` (§11), `tests/test_host_edge.py`
    - Build: Caddy with on-demand TLS that asks the worker (only live apps get a certificate); the legal pages and the report link served by the edge for every app; `DOCKER-USER` rules (no private networks, no cloud metadata, no port 25, no other apps, rate limits); `forge-host-worker doctor` checks `runsc`, user namespaces, firewall, Caddy and disk. German guide §11: the server, gVisor, DNS, the apps domain (with its entry in the Public Suffix List) and the host token.
    - Tests: the TLS question is answered yes only for live apps; the edge's legal paths win over the app's own; the firewall rules for each case; `doctor` names every missing piece with its fix.
    - Verify: `uv run pytest -q tests/test_host_edge.py`
- [x] **W26a — Deploy state machine (the server)**
    - Files: `forge_web/hosting/{__init__,deploy,deploy_api,hosts,plan}.py`, `forge_web/db/{models.py,migrations/versions/0012_hosting.py}`, `forge_web/{settings,services,startup,app}.py`, `tests/{test_hosting_deploy,test_access}.py`
    - Build: one stored deploy per environment, **staging** first, then production: the approved commit packed (`git.archive`) → the host's own checks (tests, migrations) → creator approval → admin approval (skipped for trusted users) → secrets sealed to the host's key → `pg_dump` → migrate → switch → health → live URL. Only a commit the user approved with GO_LIVE (`Report.ready_to_host`, S67b) can be deployed. Deploys go on after a restart, never migrate twice and roll back on a failed health check.
    - Tests: staging then production end to end against the fake host; nothing goes live without both approvals (trusted users: the creator's alone); a restart in the middle goes on without migrating twice; a failed health check rolls back and says why; secrets are sealed and fetched once; another commit is refused; `test_access.py` rules for every new route.
    - Verify: `uv run pytest -q tests/test_hosting_deploy.py tests/test_access.py`
- [x] **W26b — Deploys on the host**
    - Files: `forge_hostworker/{checks,runner,postgres}.py`, `docker/runtime-{python,node,static}.Dockerfile`, `../.github/workflows/forge-web-release.yml`, `tests/test_hostworker.py`
    - Build: the host runs W26a's new job kinds: `check` (the release built with its dev dependencies in a throwaway gVisor container, a throwaway PostgreSQL on a throwaway network, every service's migrations and tests, everything removed afterwards, one `CheckState` per service and kind), `backup` (`pg_dump` of the app's database into the app's folder on the host, the newest few kept), `migrate` (the release built and its migrations run against the app's database; the build is kept for the release job of the same release); the app's files in a volume of their own at `/data` when `storage_gb` is set; runtime images for `python3.12` (uv), `node22` and `static` (Node for the build, a small file server on `$PORT` for `/srv`), built by the release workflow with fixed versions.
    - Tests (the Docker stand-in): a check runs migrations and tests on a throwaway database and leaves nothing behind; failing tests are a failed check with their log; a backup dumps the database; a migrate job runs the migrations once and the release job of the same release does not build again; the files volume is mounted only for services that are not static; the runtime Dockerfiles pin their versions.
    - Verify: `uv run pytest -q tests/test_hostworker.py`
- [ ] **W27 — Go-live page and admin hosting panel**
    - Files: `frontend/src/pages/{GoLive,AdminHosting}.tsx` (+ tests), `frontend/src/api/hosting.ts`, `forge_web/hosting/admin.py`, `tests/{test_hosting_admin,test_access}.py`
    - Build: for the creator: the reviews, the checks, the staging link and "Live schalten"; for the admin: hosts and their tokens, the approval queue (showing the checked commit and its checks), trusted users, suspending an app, logs.
    - Tests: the creator sees the reviews and checks and can go live only after staging is healthy; the queue shows exactly the checked commit; marking a user trusted skips the admin step for their next deploy; a suspended app is taken off the edge; only admins reach the panel.
    - Verify: `uv run pytest -q tests/test_hosting_admin.py tests/test_access.py && (cd frontend && npm run typecheck && npm test)`
- [ ] **W28 — App services: mail, legal pages, abuse**
    - Files: `forge_web/hosting/{mail_relay,legal_pages,abuse}.py`, `forge_web/db/{models.py,migrations/versions/0013_app_services.py}`, frontend pages, `tests/{test_mail_relay,test_legal_pages,test_abuse,test_access}.py`
    - Build: a mail relay per app with quotas and a token of its own; the operator's data (Impressum, privacy contact) required before go-live and rendered for the edge; a DSA notice form → admin review → suspension with a statement of reasons to the creator.
    - Tests: an app's token sends only within its quota and only as its own sender; go-live is refused without operator data; the legal pages show the creator's data; a notice reaches the admin, and a suspension records and sends its reasons.
    - Verify: `uv run pytest -q tests/test_mail_relay.py tests/test_legal_pages.py tests/test_abuse.py tests/test_access.py`
- [ ] **W29 — Operations**
    - Files: `forge_hostworker/{backup,metrics}.py`, `forge_web/hosting/{ops,gdpr}.py`, `tests/{test_host_ops,test_hosting_gdpr}.py`
    - Build: nightly encrypted backups off the host and a restore drill; metering per app (CPU, RAM, disk, traffic); mail alerts; export and full deletion of an app (containers, database, backups, certificates).
    - Tests: a backup is encrypted before it leaves the host and the drill restores it; metering adds up per app; an alert goes out once per incident; deletion leaves nothing behind and the export holds the database and the files.
    - Verify: `uv run pytest -q tests/test_host_ops.py tests/test_hosting_gdpr.py`
- [ ] **W30 — Custom domains**
    - Files: `forge_web/hosting/domains.py`, `forge_hostworker/edge.py`, `tests/test_domains.py`
    - Build: a customer's own domain for an app, checked by a DNS TXT record before the edge asks for a certificate.
    - Tests: without the TXT record no certificate is asked for; a verified domain serves the app with the edge's legal pages; a domain belongs to one app only.
    - Verify: `uv run pytest -q tests/test_domains.py tests/test_host_edge.py`

## Phase E · Billing: customers pay the admin

Strangers pay only once the legal texts exist (from the admin's lawyer and tax adviser); Forge supplies the mechanics. Stripe runs in test mode first; a live test (`pytest -m live`, Stripe test mode) runs only with keys as GitHub secrets.

- [ ] **W31 — Stripe client and webhooks**
    - Files: `forge_web/billing/{__init__,stripe_client,webhooks,events}.py`, `forge_web/db/{models.py,migrations/versions/0014_billing.py}`, `tests/{stripe_standin,test_stripe_client,test_stripe_webhooks}.py`
    - Build: an `httpx` client for Stripe with idempotency keys; webhooks checked by HMAC and a timestamp window; every event stored once and handled in order; keys in the vault; test mode first.
    - Tests (against the stand-in): a retried call is not repeated; a wrong signature or an old timestamp is refused; a repeated event is handled once; events out of order are handled in order; keys are never logged or shown again.
    - Verify: `uv run pytest -q tests/test_stripe_client.py tests/test_stripe_webhooks.py`
- [ ] **W32 — Catalog (admin)**
    - Files: `forge_web/billing/catalog.py`, `frontend/src/pages/AdminBilling.tsx` (+ test), `tests/{test_billing_catalog,test_access}.py`
    - Build: plans (monthly, yearly), a one-time price per app, hosting per app and month, add-ons, trials and coupons, all set by the admin and synced to Stripe.
    - Tests: syncing twice changes nothing; a removed plan is archived at Stripe, not deleted; a price change makes a new price; only admins edit the catalog.
    - Verify: `uv run pytest -q tests/test_billing_catalog.py tests/test_access.py`
- [ ] **W33a — Checkout and entitlements**
    - Files: `forge_web/billing/{checkout,entitlements}.py`, `forge_web/quotas.py`, `forge_web/gateway/meter.py`, `frontend/src/pages/Billing.tsx` (+ test), `tests/{test_checkout,test_entitlements,test_access}.py`
    - Build: Stripe Checkout (no card data on the server); the order button's text per §312j BGB; the waiver of the right of withdrawal for digital content (a box and a confirmation mail); Stripe Tax. One entitlement engine sets every limit: projects, AI budget per month **and per product**, Mac minutes, hosted apps, storage, custom domains, store publishing.
    - Tests: a paid checkout grants its entitlements, a cancelled one grants nothing; checkout is refused without the waiver box; the gateway stops a product at its own budget; every limit comes from the engine.
    - Verify: `uv run pytest -q tests/test_checkout.py tests/test_entitlements.py tests/test_access.py`
- [ ] **W33b — Cancellation and dunning**
    - Files: `forge_web/billing/{cancel,dunning}.py`, frontend pages, `tests/{test_cancel,test_dunning}.py`
    - Build: the §312k cancellation button (two steps and a confirmation mail), Stripe's Customer Portal; unpaid → grace period → suspended → deleted after notice and an export.
    - Tests: cancelling takes two steps without signing in again and sends the mail; every dunning stage at its date; nothing is deleted before the notice and the export.
    - Verify: `uv run pytest -q tests/test_cancel.py tests/test_dunning.py`
- [ ] **W34 — Legal setup**
    - Files: `forge_web/legal.py`, `forge_web/db/{models.py,migrations/versions/0015_legal.py}`, frontend pages, `tests/{test_legal,test_access}.py`
    - Build: versioned AGB, privacy policy, data processing agreement (with TOMs and subprocessors) and AUP; every acceptance stored; a new version must be accepted again; the EU VAT ID checked (VIES). The texts come from the admin; Forge supplies only the mechanics.
    - Tests: nothing paid is possible before acceptance; a new version asks again; acceptances name the version and the time; a VAT ID is checked against the VIES stand-in.
    - Verify: `uv run pytest -q tests/test_legal.py tests/test_access.py`

## Phase F · Payments in the apps

The apps take money from their own users. The template's payments module (S69) and the Apple client for the API (S70) are built in Forge on `main`; order: W35 → S69 → S70 → W36.

- [ ] **W35 — Stripe Connect and payments relay**
    - Files: `forge_web/billing/{connect,relay}.py`, `tests/{stripe_standin_connect,test_connect,test_payments_relay}.py`
    - Build: creators onboard as Stripe Connect Express accounts; the relay acts for exactly one connected account per app token and takes the admin's application fee; the platform key never leaves the server.
    - Tests: an app token reaches only its own account; the fee is on every charge; a suspended app's token is refused; the platform key is never in a response or a log.
    - Verify: `uv run pytest -q tests/test_connect.py tests/test_payments_relay.py`
- [ ] **W36 — Apple in-app purchases on the server**
    - Files: `forge_web/apple/iap.py`, `tests/{asc_standin,test_apple_iap}.py`
    - Build: App Store Server Notifications v2 (JWS chain checked with `cryptography`); renewals and refunds update the app's entitlements. It connects the Apple path (W22) to the hosted backend.
    - Tests: a valid chain is accepted, a broken or foreign one refused; a renewal extends and a refund removes the entitlement; a repeated notification counts once.
    - Verify: `uv run pytest -q tests/test_apple_iap.py`

## Phase G · More platforms

Android before Windows (it builds on Linux and costs less). The templates are built in Forge on `main` (S71, S72).

- [ ] **W37 — Android** (with S71)
    - Files: `forge_hostworker/android.py`, `forge_web/stores/{__init__,play}.py`, `tests/{play_standin,test_android_build,test_play_release}.py`
    - Build: a Gradle build in a Linux container; the Play Developer API with a service-account JWT (RS256); release only after the review gate. New personal Play accounts need a 14-day closed test with 12 testers; an organisation account (D-U-N-S) avoids it.
    - Tests: the build runs in a throwaway container without secrets; every token is checked by the stand-in; nothing is released without the approval; a personal account is told about the closed test.
    - Verify: `uv run pytest -q tests/test_android_build.py tests/test_play_release.py`
- [ ] **W38 — Windows** (with S72)
    - Files: `packages/winworker/` (the Mac-worker protocol on a Windows machine), `forge_web/stores/msstore.py`, `pyproject.toml` (workspace), `AGENTS.md` (package rule), `tests/{msstore_standin,test_winworker,test_msstore}.py`
    - Build: a Windows worker that builds MSIX packages; a submission through the Partner Center submission API (Entra ID); the Store signs the MSIX; direct downloads come later with Azure Trusted Signing.
    - Tests: a job goes from the server to the Windows worker and back; every token is checked by the stand-in; nothing is submitted without the approval.
    - Verify: `uv run pytest -q tests/test_winworker.py tests/test_msstore.py`
