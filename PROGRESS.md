# Progress

Next step: S03

## Done
| Step | Date | Commit | Files | Notes |
| --- | --- | --- | --- | --- |
| S01 | 2026-10-05 | e06bc18 | pyproject.toml, src/forge/__init__.py, src/forge/__main__.py, src/forge/cli.py, tests/test_smoke.py, .github/workflows/ci.yml, PROGRESS.md, .gitignore, .python-version | `forge --version` prints `forge 0.1.0`; CI matrix ubuntu/macos/windows runs the gate |
| S02 | 2026-10-05 | (next) | src/forge/config.py, src/forge/toml_writer.py, forge.example.toml, tests/test_config.py, src/forge/cli.py | layering, env overrides, trust gate, profiles, `forge config check` with masked secrets |

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

## Open issues
- None yet.
