# Security

Forge runs a language model that can read files, change them and run commands. This page
describes how it is contained, what the tests in `tests/security/` prove, and what is not
covered.

## Layers

| Layer | What it does | Where |
| --- | --- | --- |
| Path checks | Writes only inside the project and `sandbox.writable_roots`; `.git/` and `.forge/` are protected; paths are resolved (symlinks, `..`) before the check | `runtime/files.py` |
| Permission rules | `deny` → `ask` → `allow` → read-only list → sandbox × approval policy | `runtime/permissions.py`, `runtime/rules.py` |
| OS sandbox | Shell commands write only to the project, temp folders and `/dev`; outgoing network off | `runtime/sandbox.py` (Landlock/bubblewrap on Linux, Seatbelt on macOS) |
| Approvals | `ask` decisions go to the user; headless runs refuse unless `--yes` | renderers |
| Secret masking | Tool output is scanned for key formats and secret environment values before the model sees it | `runtime/secrets.py` |
| Trust | A project's `.forge/config.toml` may set providers, MCP servers, hooks, the release rules or the browser only after `forge trust`; before that its sandbox, approval, permission and limit settings may only be stricter than the user's, and no project drops the user's deny and ask rules | `config.py`, `config_trust.py` |
| Browser | The browser agent has no profile, no downloads and no file access; every request to a local or private host is aborted; `browser_open` asks like `web_fetch` | `local/playwright_browser.py`, `tools.py` |
| Prompt structure | Fetched pages and repo files are data; the WEB_EXTRACT prompt tells the model to ignore instructions in them | `prompts.py` |

## What the security tests check

- **Path escape** (`test_path_escape.py`): `../` and absolute paths, writes through a symlinked
  file or folder (also via `apply_patch`), the protected `.git/` and `.forge/` folders, and
  reading outside the project (also through a symlink) only after the user approves.
- **Rule bypass** (`test_rule_bypass.py`): a `deny` rule also catches the command when it is
  chained (`a; b`, `&&`, `||`, `|`, `&`, new lines), substituted (`$(...)`, `<(...)`,
  backticks), grouped (`( ... )`, `{ ...; }`, `if`/`then`/`do`, `!`), wrapped (`sh -c`,
  `bash -lc`, `bash <<< ...`, `echo ... | sh`, `pwsh -Command` also abbreviated,
  `-EncodedCommand`, `cmd /c`, `eval`, `find -exec`, `sudo -u root`, `env -u X`, `env -S`,
  `nice -n 10`, `timeout 5`, `nohup`, `xargs -n 1`, `VAR=1 cmd`) or sent to the other shell (a
  `bash(...)` rule covers `powershell` too). An `allow` rule applies only when *every* command
  in the line is allowed, so `git status; rm -rf /` is not allowed by `bash(git status*)`; a
  command run with `sudo` (or `doas`, `su` ...) or writing a file with `>` is only allowed by a
  rule that names it (`bash(sudo apt update)`). A command with a NUL byte is refused: bash
  would run what follows the byte as a command of its own.
- **Secrets** (`test_secrets.py`): OpenAI, Anthropic, GitHub, AWS, Slack, Google and GitLab
  key formats, PEM private keys and the values of secret-named environment variables are
  replaced by `[masked secret]` in every tool result (also in spill files).
- **Prompt injection** (`test_injection.py`): a model that *obeys* instructions planted in a
  README still cannot weaken its own rules: writing `.forge/config.toml` is `protected_path`,
  the user config is `outside_root`, and `remember` always asks. A fetched page reaches the
  extraction model only inside `<page>` tags, after the rule to ignore its instructions.

## Not covered (known limits)

- **Windows** has no OS sandbox yet; commands rely on approvals and path checks there
  (see PROGRESS.md, S28).
- **Landlock** cannot protect `.git/` and `.forge/` inside a writable project root; only
  Forge's own file tools enforce `protected_path`. A shell command can still write there.
- **`full-access`** mode turns the sandbox off by design.
- **Command parsing** is a best effort over shell syntax (it does not expand variables or
  aliases, e.g. `$CMD` holding `rm -rf`). Deny rules are a guard rail, not a sandbox; the OS
  sandbox is the boundary.
- **Secret masking** only knows common formats and secret-named environment variables; a
  password in an arbitrary file is not detected.
- **MCP servers and hooks** run code you configured; trust a project only if you trust its
  `.forge/config.toml`.

## Reporting

Report security problems privately to the maintainers rather than in a public issue.
