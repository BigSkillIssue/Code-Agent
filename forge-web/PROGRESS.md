# Progress

Next step: W08b

## Done
| Step | Date | Commit | Files | Notes |
| --- | --- | --- | --- | --- |
| W01 | 2026-10-07 | 1d73b6f | pyproject.toml, packages/sandbox/pyproject.toml, packages/server/pyproject.toml, forge_sandbox/{__init__,__main__,cli}.py, forge_web/{__init__,__main__,cli,app,settings}.py, tests/conftest.py, tests/test_smoke.py, tests/test_settings.py, ../.github/workflows/forge-web.yml, AGENTS.md, PROGRESS.md, README.md, docs/STEPS.md | workspace with Forge as editable path dependency; settings defaults → file → env; `/api/health` |
| W02 | 2026-10-07 | 95b2709 | forge_sandbox/{frames,protocol,mux,rpc}.py, docs/PROTOCOL.md, tests/support.py, tests/test_frames.py, tests/test_mux.py | 9-byte header frames, 1 MiB credit window per channel, management + control frames first, CLOSE stays behind data; RPC with generic `internal` errors |
| W03 | 2026-10-08 | d20353b | forge_sandbox/{daemon,attach,fsops,methods,procs,pty,netinfo,gitinfo,forward,streams,cli}.py, forge_web/containers/{__init__,driver,local}.py, forge_web/sandbox_client.py, tests/test_fsops.py, tests/test_daemon.py, AGENTS.md | daemon state outlives connections (newest connection wins); fs ops walk with O_NOFOLLOW per component; programs, terminals and git run as the workspace owner; forward out (listen → `forward` channel) and in (`connect` channel); LocalDriver spawns `--stdio` daemons with a clean environment |
| W04 | 2026-10-08 | aa2e751 | forge_sandbox/{worker,pipe_renderer,chats,history,prompts,methods,daemon,cli,streams}.py, forge_web/{dev_chat,cli}.py, forge_web/containers/local.py, tests/{support,test_worker,test_chats,test_dev_chat,test_daemon}.py, docs/PROTOCOL.md | one worker process per chat (python -I, protocol on private fds, non-dumpable); numbered chat buffer with replay after `seq`; first answer wins; a crashed worker is replaced on the next message and reopens the same Forge session; follow-ups carry the last 10 turns; `forge-web dev-chat --fake` |
| W05a | 2026-10-08 | 389b90c | forge_web/db/{__init__,engine,models,writer}.py, forge_web/db/migrations/{env.py,script.py.mako,versions/0001_initial.py}, forge_web/chats/{api,runs,items}.py, forge_web/{hub,ws,projects,access,services,fake,app,cli,settings,dev_chat}.py, forge_web/auth/{__init__,dev}.py, forge_sandbox/{daemon,gitinfo}.py, tests/{support,test_server}.py, docs/STEPS.md | single-user server: projects, chats, event log with gapless numbers, WebSocket replay without gaps or repeats, first answer wins across tabs; `serve --dev [--fake]` |
| W05b | 2026-10-08 | 853c351 | frontend/ (package.json, vite.config.ts, tsconfig.json, index.html, public/favicon.svg, src/{main,App}.tsx, src/api/{types,client,socket}.ts, src/state/{store,transcript}.ts, src/components/{Sidebar,ChatView,Transcript,Composer,Markdown}.tsx, src/components/cards/*.tsx, src/lib/{i18n,tools}.ts, src/pages/Login.tsx, src/styles.css, tests), forge_web/{webui,app}.py, tests/test_webui.py, ../.github/workflows/forge-web.yml, README.md | React 19 + Vite 6 + Tailwind 4 app: sidebar of projects and chats, streaming transcript with tool, approval, question, plan and report cards, German/English, light/dark; served with an SPA fallback and a strict CSP; checked end to end in Chromium |
| W06 | 2026-10-08 | f6ad99d | docker/sandbox.Dockerfile, docker/sandbox.Dockerfile.dockerignore, forge_web/containers/docker.py, forge_web/{sandbox_cli,settings,app,services,cli}.py, forge_web/chats/runs.py, tests/{support,test_docker_driver}.py, ../.github/workflows/forge-web.yml | one container per project: daemon as PID 1 under --init, volumes for /workspace and /home/forge, read-only root, no network, all capabilities dropped but six, no-new-privileges, CPU/memory/PID/nofile limits, gVisor when installed; attach over `docker exec`; chats resume after a server restart; idle containers stop; `forge-web sandbox build`; real-Docker tests pass locally (4) |
| W07 | 2026-10-08 | e70f4bb | forge_web/gateway/{__init__,tokens,upstreams,meter,keys,proxy,api}.py, forge_web/{egress,vault,startup,app,services,settings}.py, forge_web/chats/runs.py, forge_web/containers/docker.py, forge_web/db/models.py, forge_web/db/migrations/versions/0002_gateway.py, tests/{test_gateway,test_gateway_e2e,test_egress,test_docker_driver}.py | model calls go sandbox → daemon forward → private gateway (unix socket) → provider with the real key; signed run tokens valid only while their chat works; only model endpoints; output capped; usage from the upstream's reply (estimate if aborted); user keys win, server keys need a grant and a monthly limit; egress CONNECT proxy with an allow list and public addresses only; e2e test with Forge's real Anthropic client |
| W08a | 2026-10-08 | (next) | forge_web/auth/{passwords,sessions,onetime,origin,ratelimit,mail,routes,dev}.py, forge_web/{audit,user_cli,startup,services,settings,app,cli,projects,ws}.py, forge_web/chats/api.py, forge_web/gateway/{api,proxy}.py, forge_web/db/{models.py,migrations/versions/0003_accounts.py}, frontend/src/{App.tsx,api/client.ts,pages/Auth.tsx,components/Sidebar.tsx,lib/i18n.ts}, tests/{support,test_auth,test_user_cli,test_server,test_docker_driver,test_gateway_e2e,test_dev_chat}.py | real accounts: argon2id, hashed server-side sessions, CSRF value per session, Origin checks (API + WebSocket), first-admin setup link, invite/approval/open sign-up, one-time links, rate limits, audit log, user CLI; checked in Chromium |

## Decisions
- Session: the user asked for Forge Web as a separate part on a separate branch without touching Forge. It lives in `forge-web/` on branch `claude/elegant-tesla-h2iugo`; Forge's `src/` and `tests/` stay unchanged. Forge's AGENTS.md rule 3 ("no server code") is overridden by the user's instruction for `forge-web/` only; the root AGENTS.md/CLAUDE.md got a paragraph saying so.
- Session: all steps are built one after another in this session (user asked for the full program); every step still gets tests, a gate run, a PROGRESS entry and its own commit.
- W01: the Commit column is filled in by the following step's commit, as in Forge.
- W01: mypy finds Forge through `mypy_path = ../src` because Forge ships no `py.typed` marker (adding one would change Forge).
- W01: `forge-web serve` runs uvicorn inside `asyncio.run` so Windows keeps the Proactor loop that subprocesses need; one process only, because run state lives in memory.
- W01: settings env overrides use `FORGE_WEB_<SECTION>__<KEY>`; lists and tables are given as JSON; `FORGE_WEB_DATA_DIR` and `FORGE_WEB_CONFIG` are reserved.

- W02: `rpc.py` added beside `mux.py` (the card named only `mux.py`): request/response bookkeeping is its own responsibility.
- W02: the receiver grants credit when half the window was consumed or when nothing is left to read; without the second rule a message larger than the rest of the window could wait forever.
- W02: JSON messages are capped at the window size (1 MiB); larger content goes over byte-stream channels.

- W03: the daemon refuses to follow symbolic links at all (it may run as root in a container); the agent itself still handles links inside its own process.
- W03: in Docker the daemon will run as container root with only the capabilities it needs to switch to the workspace owner, so the agent (uid 1000) cannot open its socket and answer its own approvals; in local mode the daemon is a `--stdio` child of the server, so there is no socket at all.
- W03: `methods.py` (parameter models) and `streams.py` (pump, stdio streams) are shared wire modules; AGENTS.md rule 2 lists what the server may import.
- W03: terminals are POSIX-only for now (the sandbox is a Linux container); Windows local mode reports `not_supported` until W13.
- W03: `procs.start` keeps the last 5000 lines per program and cuts unterminated lines at 4000 characters.

- W04: chat modes map onto Forge's approval policy: ask → `always` (every change asks), edits → `on-request` (Forge's default: edits run, unsandboxed commands ask), auto → `never` plus allow rules for the tools that would otherwise be denied (web_fetch, web_search, remember, browser_open); plans are approved automatically only in auto mode.
- W04: the chat's overrides always set `approval.policy`, `permissions.allow` and `sandbox.mode`, so a repository's own `.forge/config.toml` cannot loosen them.
- W04: Forge publishes no role with its events, so the refiner's JSON spec streams as model text (Forge's CLI shows it too); the web UI shows such structured replies as a folded card (W10).
- W04: chat channel messages are tagged (`hello`, `gap`, `item`); items are cut to fit one message (long strings like screenshots are replaced by a note).
- W04: Forge's import takes 2-3 s, so most worker tests run the worker in the test process; test_chats.py runs real worker processes.

- W05: W05 split into W05a (server) and W05b (web UI scaffold).
- W05a: model text (`model_delta`) and live command output (`tool_output`) are streamed but not stored: `model_done` and `tool_finished` carry the same content and are. A subscribe gets the partial text and output of a running turn in its `subscribed` message.
- W05a: stored items get the server's own gapless numbers; the daemon's numbers (`dseq`) are stored with them, plus the daemon's boot id per chat, so after a reconnect the relay asks for everything after the last stored daemon number, and after a daemon restart from 1.
- W05a: until accounts exist (W08), `forge-web serve` only runs with `--dev` (local isolation, one admin, a login link on stderr).
- W05a: a WebSocket without a valid session is refused during the handshake (HTTP 403).
- W05a: projects use 16-hex ids, chats 32-hex ids; both fit the sandbox id pattern.
- W05a: the server tests start a real uvicorn server in a thread and talk HTTP + WebSocket to it with timeouts.

- W05b: rollup is pinned to 4.44.1 through `overrides`: npm resolved vite's `^4` to rollup 4.64.2 (released the day before), whose tree-shaking hangs on react-dom.
- W05b: the built UI is not committed; `npm run build` writes it into `forge_web/static/` (git-ignored) and the CI job builds it. Without a build, the server shows how to build it.
- W05b: replies that are one JSON object (Forge's task spec, reviews) are shown as a folded "task understood" card; a trivial task's summary is not repeated under its reply.
- W05b: the server module is `webui.py`, not `static.py`, so it cannot clash with the `static/` folder it serves.

- W06: inside the container the daemon runs as root with six capabilities (CHOWN, DAC_OVERRIDE, FOWNER, SETUID, SETGID, KILL) so its socket stays out of reach of the agent's user (uid 1000) and it can start programs as that user; the test checks that the agent gets PermissionError on the socket. gVisor (`runsc`) is used automatically when Docker has it; SECURITY.md (W17) will recommend it and rootless Docker / userns-remap.
- W06: Forge lives in /opt/forge (its own venv, not on the user's PATH), so a project's pip install never touches it; the image is about 1.8 GB (Python, node, git, ripgrep, build tools).
- W06: the container has `--network none`; reaching models (W07) and package registries (W07 egress proxy) goes through the daemon's forward channels.
- W06: a server shutdown closes only its `docker exec` connections: containers, chats and open approvals keep going, and the next server follows the chats that were running or waiting (`resume_active`).
- W06: project containers stop after `sandbox.idle_minutes` without use (no chat running or waiting); their volumes stay.
- W06: Docker tests are marked `docker` and need the image `forge-web-sandbox:dev` (or $FORGE_WEB_TEST_IMAGE); CI builds it first. In this environment the image builds with `--network host --build-arg HTTPS_PROXY=… --secret id=ca,src=<proxy CA>` (only HTTPS goes through the agent proxy).

- W07: the master secret is a file in the data folder (`secret.key`, mode 0600), never in the database; API keys are stored encrypted (Fernet via MultiFernet, ready for rotation) and run tokens are HMAC-signed with a derived key, so they survive restarts without being stored. Bumping `chats.token_generation` revokes a chat's token.
- W07: the gateway listens on a private unix socket in the data folder (loopback TCP on Windows) and is reached only through daemon forward channels; its token check also requires the chat to be running or waiting, so a leaked token is useless between turns.
- W07: the run token travels in `FW_GATEWAY_TOKEN`, not `FORGE_*`, because Forge reads `FORGE_*` variables as config overrides. Forge's secret masking hides it in tool output; the agent's own commands could still read it (same user), which only lets them spend the chat owner's budget while the chat runs.
- W07: unknown models are priced at $5/$25 per million tokens on server keys, so limits still bite; cost is reserved before a call (input estimate + capped output) and settled from the upstream's usage.
- W07: the egress proxy needs no listening port (socketpair per connection); when the server itself has HTTPS_PROXY, CONNECTs are chained through it.
- W07: only provider kinds anthropic, openai_compat (incl. Ollama, LM Studio, vLLM, OpenRouter, Groq, …) and google go through the gateway; LiteLLM, Bedrock and Vertex do not.

- W08: W08 split into W08a (accounts, sessions) and W08b (members, admin API, access matrix).
- W08a: requests without an Origin header (scripts, tests, the CLI) pass the Origin check: only browsers can be tricked into cross-site requests, and they always send Origin; the CSRF value still guards every changing request of a session.
- W08a: in local isolation (not dev mode) sign-up needs an invite, and the server warns that every user can run commands as the server's user.
- W08a: when the data folder's path is too long for a unix socket (~100 bytes), the gateway socket goes into a private temp folder (0700).
- W08a: rate limits live in the app's services (no module-level state).
- W08a: the suite stays under 60 s by running the CLI tests in-process.

## Open issues
- (none)
