# Progress

Next step: W05

## Done
| Step | Date | Commit | Files | Notes |
| --- | --- | --- | --- | --- |
| W01 | 2026-10-07 | 1d73b6f | pyproject.toml, packages/sandbox/pyproject.toml, packages/server/pyproject.toml, forge_sandbox/{__init__,__main__,cli}.py, forge_web/{__init__,__main__,cli,app,settings}.py, tests/conftest.py, tests/test_smoke.py, tests/test_settings.py, ../.github/workflows/forge-web.yml, AGENTS.md, PROGRESS.md, README.md, docs/STEPS.md | workspace with Forge as editable path dependency; settings defaults → file → env; `/api/health` |
| W02 | 2026-10-07 | 95b2709 | forge_sandbox/{frames,protocol,mux,rpc}.py, docs/PROTOCOL.md, tests/support.py, tests/test_frames.py, tests/test_mux.py | 9-byte header frames, 1 MiB credit window per channel, management + control frames first, CLOSE stays behind data; RPC with generic `internal` errors |
| W03 | 2026-10-08 | d20353b | forge_sandbox/{daemon,attach,fsops,methods,procs,pty,netinfo,gitinfo,forward,streams,cli}.py, forge_web/containers/{__init__,driver,local}.py, forge_web/sandbox_client.py, tests/test_fsops.py, tests/test_daemon.py, AGENTS.md | daemon state outlives connections (newest connection wins); fs ops walk with O_NOFOLLOW per component; programs, terminals and git run as the workspace owner; forward out (listen → `forward` channel) and in (`connect` channel); LocalDriver spawns `--stdio` daemons with a clean environment |
| W04 | 2026-10-08 | (next) | forge_sandbox/{worker,pipe_renderer,chats,history,prompts,methods,daemon,cli,streams}.py, forge_web/{dev_chat,cli}.py, forge_web/containers/local.py, tests/{support,test_worker,test_chats,test_dev_chat,test_daemon}.py, docs/PROTOCOL.md | one worker process per chat (python -I, protocol on private fds, non-dumpable); numbered chat buffer with replay after `seq`; first answer wins; a crashed worker is replaced on the next message and reopens the same Forge session; follow-ups carry the last 10 turns; `forge-web dev-chat --fake` |

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

## Open issues
- (none)
