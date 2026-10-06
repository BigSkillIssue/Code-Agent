# Forge — Tool reference

Forge v1 ships 30 built-in tools (more since v1.0, e.g. `research`) in 8 groups, all in `tools.py`; MCP servers add more at runtime. Every tool has a spec card with six parts, and the implementation must match it exactly: **Signature** (the Python definition), **Inputs** (types, defaults, validation), **Behaviour** (the algorithm, in order), **Output** (exact text the model receives), **Errors** (codes from the shared list), **Tests** (minimum tests for the step card).

## Shared mechanics

Every tool passes through the same six stages, so behaviour is predictable and each tool body stays short.

1. **Schema.** `@tool` reads the function signature and docstring and builds the JSON schema; each provider adapter converts it to its own format (or to the prompt-based fallback for models without tool calling).
2. **Validate.** Arguments are parsed with Pydantic. Invalid arguments return an error result that names the field, so the model can retry.
3. **Permission.** `permissions.check(tool, args)` applies `deny` → `ask` → `allow` rules, then sandbox mode and approval policy. Result: run, ask the user, or refuse with a reason.
4. **Hooks.** `pre_tool` hooks run; exit code 2 blocks the call and its stderr goes back to the model.
5. **Run.** The body runs inside the `Executor` (sandboxed for shell commands) with the session's `Ctx`: cwd, shell sessions, read ledger, store, event bus.
6. **Result.** Text back to the model, an event to the renderer, `post_tool` hooks, an audit-log line.

## Output conventions (all tools)

- **Return type.** Every tool returns `ToolResult(call_id, ok, text, code, spill_path, images)`. `ok=True` means the tool did what was asked; `ok=False` means it did not, and `code` holds one error code from the table below.
- **Error text.** When `ok=False`, the first line of `text` is exactly `error[<code>]: <message>`. Optional further lines: `hint: <what to try instead>`, then any tool-specific body (for example the output of a failed command).
- **Paths in output** are relative to the project root with forward slashes on every OS (`src/forge/agent.py`). Paths outside the root are shown absolute.
- **Path inputs** accept relative paths (resolved against `ctx.cwd`), absolute paths, and `~`. After resolving symlinks, a path outside the root and outside `sandbox.writable_roots` is `outside_root` for writes and needs approval for reads.
- **Line-numbered text** uses `cat -n` style: the line number right-aligned in 6 columns, a tab, then the line (`f"{n:>6}\t{line}"`).
- **Sizes** are printed as `412 B`, `4.1 KB`, `2.3 MB`; durations as `0.4s`, `61.0s`.
- **Capping** happens after the tool returns, in `call_tool`: success text over 30,000 characters is saved to `.forge/out/<call_id>.txt` and replaced by its first 2,000 characters plus the line `[output truncated: <N> chars total, full output in .forge/out/<call_id>.txt]`. Failure text over 10,000 characters keeps the first and last 5,000 characters joined by `\n[... <N> chars omitted ...]\n`.
- **No colours, no progress bars.** Every subprocess runs with `NO_COLOR=1`, `TERM=dumb`, `CI=1`, `PAGER=cat`, `GIT_PAGER=cat` and stdin closed.

**Shared error codes** (use only these; add a new one only via an *Open issue*):

| Code | Meaning |
|---|---|
| `invalid_args` | Arguments fail validation or contradict each other |
| `not_found` | File, job, agent, task or resource does not exist |
| `outside_root` | Write target outside the project and writable roots |
| `protected_path` | `.git/`, `.forge/` or a path matched by a `deny` rule |
| `permission_denied` | A rule denied it, or the user declined the approval |
| `sandbox_denied` | The OS sandbox blocked it (network, path) |
| `not_read` | File must be read with `read_file` before this change |
| `stale` | File changed on disk since the last read and the change cannot be applied safely |
| `no_match` | Text, hunk or pattern to change was not found |
| `not_unique` | Text to change was found more than once |
| `binary_file` | File is binary and the tool only handles text |
| `too_large` | Input or file exceeds a size limit |
| `timeout` | Operation did not finish in time |
| `exit_nonzero` | Command finished with a failing exit code |
| `check_failed` | A step's check did not pass |
| `http_status` | Web request returned 4xx or 5xx |
| `network` | DNS, connection or TLS failure |
| `unsupported` | Not available here (no bash, no vision, wrong role) |
| `busy` | Resource held by another agent |
| `limit_reached` | A configured limit (turns, attempts, cost, searches, agents) was hit |
| `cancelled` | The user or the parent cancelled it |
| `tool_error` | An MCP tool reported an error |

**Contract change.** `ToolResult` in `docs/CONTRACTS.md` therefore has two extra fields: `code: str | None = None` and `images: list[ImagePart] = []`.

## Shell

All four tools use `runtime/shell.py` through `ctx.executor`. Each session has at most one persistent bash process and one persistent PowerShell process, started lazily on first use.

### `bash`

```python
@tool(group="shell", permission="ask", read_only=False, specifier_arg="command")
async def bash(
    ctx: Ctx,
    command: Annotated[str, "The bash command to run. Multi-line scripts allowed."],
    timeout_s: Annotated[int, "Seconds before the command moves to the background (max 600)."] = 120,
    background: Annotated[bool, "Start as a background job and return immediately."] = False,
    description: Annotated[str, "Up to 80 chars shown to the user, e.g. 'Run unit tests'."] = "",
) -> ToolResult:
    """Run a command in the session's persistent bash shell and return exit code, stdout and stderr."""
```

| Input | Type | Default | Rules |
|---|---|---|---|
| `command` | str | required | 1–100,000 chars, not only whitespace |
| `timeout_s` | int | 120 | 1–600; `limits` may lower the max |
| `background` | bool | False |  |
| `description` | str | "" | max 80 chars, single line |

**Behaviour**

1. Validate inputs. Find bash: `bash` on `PATH`; on Windows also `C:\Program Files\Git\bin\bash.exe`. None found → `unsupported`.
2. Permission: split `command` on `&&`, `||`, `;`, `|` and newlines (respecting quotes, via `shlex`). If every segment's program is in `READ_ONLY_COMMANDS` (`ls cat head tail wc pwd echo which file stat rg grep find git-status git-diff git-log git-show`) and there is no `>`, `>>` or `tee`, the call is auto-approved; otherwise rules, then sandbox × approval policy decide.
3. If `background=True`: start a new process (not the persistent shell) in `ctx.cwd`, stdout and stderr both appended to `.forge/jobs/<job_id>.log`; job ids are `j1`, `j2`, … per session. Return the background output (below).
4. Otherwise send to the persistent shell, with a fresh 16-hex nonce: `cd -- '<cwd>' && { <command>\n} 2>'<tmp_stderr>'; printf '\n__FORGE_<nonce>__ %d %s\n' "$?" "$PWD"` Read stdout until the sentinel line; parse exit code and the new working directory; read stderr from the temp file, then delete it.
5. If the sentinel does not appear within `timeout_s`: detach the running process as a background job (its output keeps going to its log), start a fresh persistent shell, return the timeout output. Commands starting with `sleep` are killed instead.
6. New cwd inside the root → `ctx.cwd` = new cwd. Outside → keep the old cwd and add the line `note: cwd reset to <root-relative cwd>`.
7. `ok = exit_code == 0` or (exit code 1 and the first program is in `BENIGN_EXIT1` = `grep rg egrep fgrep find diff test [ git-diff git-grep`). If `executor` reports `sandbox_denied`, return that error instead.

**Output** (sections with empty content are omitted; `cwd` is printed only when it changed):

```
exit_code: 0
duration: 1.4s
cwd: src
--- stdout ---
<stdout>
--- stderr ---
<stderr>
```

Failure prefixes the same body with `error[exit_nonzero]: exit code 2`. Background start:

```
started job j3 (pid 41233): <description or first 60 chars of command>
log: .forge/jobs/j3.log
read output with job_output("j3"); stop with job_stop("j3")
```

Timeout (ok=True, because the command is still running):

```
timeout: still running after 120s, moved to background as job j4
read output with job_output("j4"); stop with job_stop("j4")
--- stdout so far ---
<stdout>
```

**Errors:** `invalid_args`, `unsupported` (no bash), `permission_denied` (rule or user; the user's feedback follows as `hint:`), `sandbox_denied` (`hint: the sandbox blocked <network|path>; ask the user or use another approach`), `exit_nonzero`.

**Tests:** echo to stdout and stderr; non-zero exit gives `exit_nonzero`; `grep` with no match is ok; `cd src` persists to the next call; `cd /` resets with a note; a 2 s timeout moves `python -c "import time; time.sleep(5)"` to the background; background job log fills; env var `CI=1` is set.

### `powershell`

```python
@tool(group="shell", permission="ask", read_only=False, specifier_arg="command")
async def powershell(
    ctx: Ctx,
    command: Annotated[str, "The PowerShell command or script to run."],
    timeout_s: Annotated[int, "Seconds before the command moves to the background (max 600)."] = 120,
    background: Annotated[bool, "Start as a background job and return immediately."] = False,
    description: Annotated[str, "Up to 80 chars shown to the user."] = "",
) -> ToolResult:
    """Run a command in the session's persistent PowerShell and return exit code, stdout and stderr."""
```

Inputs, output format, errors and steps 3, 5, 6 are identical to `bash`. Differences:

1. Executable: `pwsh` (PowerShell 7+) if on `PATH`; on Windows otherwise `powershell.exe` (5.1); neither → `unsupported`. Started as `<exe> -NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass -Command -`.
2. At session start, run `[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false); $OutputEncoding = [Text.UTF8Encoding]::new($false); $ProgressPreference = 'SilentlyContinue'`.
3. Wrapper: `Set-Location -LiteralPath '<cwd>'; $global:LASTEXITCODE = 0; try { <command> } catch { Write-Error $_ }; $c = if ($?) { $LASTEXITCODE } elseif ($LASTEXITCODE) { $LASTEXITCODE } else { 1 }; "\n__FORGE_<nonce>__ $c $((Get-Location).Path)"`. stderr is captured from the error stream, redirected with `2>` to the temp file.
4. `READ_ONLY_COMMANDS` for PowerShell: `Get-ChildItem Get-Content Get-Location Get-Item Select-String Test-Path Resolve-Path Measure-Object` and their aliases (`ls dir gci cat type gc pwd sls`), plus the git subcommands from `bash`.
5. `BENIGN_EXIT1`: `findstr`, `where.exe`, `fc.exe` when they printed output; `git diff`, `git grep`; `robocopy` exit codes 0–7 count as success.
6. A `deny` rule on `bash(...)` also applies to the same command text in `powershell(...)`, so a denied command cannot be run through the other shell.

**Tests:** `Write-Output` and `Write-Error` land in the right sections; `exit 3` gives exit code 3; `Set-Location` persists; non-ASCII text round-trips as UTF-8; `robocopy` exit 1 is ok; skipped with a reason when no PowerShell is installed.

### `job_output`

```python
@tool(group="shell", permission="auto", read_only=True)
async def job_output(
    ctx: Ctx,
    job_id: Annotated[str, "Job id, e.g. 'j3'."],
    since_line: Annotated[int, "First line to return (0-based)."] = 0,
    wait_s: Annotated[float, "Wait up to this long for new output or exit (max 30)."] = 0,
    max_lines: Annotated[int, "Maximum lines to return (max 2000)."] = 400,
) -> ToolResult:
    """Read output of a background job and report whether it is still running."""
```

| Input | Rules |
|---|---|
| `job_id` | matches `^j\d+$` and belongs to this session |
| `since_line` | ≥ 0 |
| `wait_s` | 0–30 |
| `max_lines` | 1–2000 |

**Behaviour:** look up the job; if `wait_s > 0`, poll every 0.25 s until the log has lines past `since_line`, the job exits, or `wait_s` passes; read lines `since_line` to `since_line + max_lines` from the log.

**Output:**

```
job j3: running (pid 41233, 52.3s)
lines 120-163 of 163; next since_line: 164
--- output ---
<lines>
```

Finished jobs show `job j3: exited with code 0 after 61.0s` as the first line. No new lines → `--- output ---` is followed by `(no new output)`.

**Errors:** `invalid_args`, `not_found` (`hint: known jobs: j1, j2`).

**Tests:** returns new lines only after `since_line`; `wait_s` returns early when output arrives; finished job shows its exit code; unknown id lists known ids.

### `job_stop`

```python
@tool(group="shell", permission="auto", read_only=False)
async def job_stop(ctx: Ctx, job_id: Annotated[str, "Job id, e.g. 'j3'."]) -> ToolResult:
    """Stop a background job and its child processes."""
```

**Behaviour:** POSIX: send SIGTERM to the job's process group, wait up to 5 s, then SIGKILL. Windows: `taskkill /PID <pid> /T`, wait up to 5 s, then `taskkill /PID <pid> /T /F`. Then read the last 20 log lines. Jobs are also stopped this way when the session ends.

**Output:**

```
job j3 stopped (SIGTERM, after 52.3s)
--- last 20 lines ---
<lines>
```

Already finished: `job j3 had already exited with code 0` (ok=True).

**Errors:** `invalid_args`, `not_found`.

**Tests:** a `sleep 60` job stops within 6 s; its child process is gone too; stopping a finished job is ok.

## Files

All writes go through one function, `runtime/files.py: apply_changes(ctx, changes: list[FileChange]) -> None`, which checks protected paths, records the old content for `/undo`, writes atomically (temp file in the same folder + `os.replace`) and updates the read ledger. The ledger (`runtime/ledger.py`) maps each resolved path to `LedgerEntry(sha256: str, mtime_ns: int, full: bool)`.

**Text handling for all file tools:** decode as UTF-8 (strip and remember a BOM); if that fails, decode as Latin-1 and add the line `note: file is not valid UTF-8, read as Latin-1`. Detect the line ending style from the first line break (`\r\n` or `\n`); writes keep the file's style and BOM. A file is binary if its first 8 KB contain a NUL byte.

### `read_file`

```python
@tool(group="files", permission="auto", read_only=True, specifier_arg="path")
async def read_file(
    ctx: Ctx,
    path: Annotated[str, "File to read, relative to the current directory or absolute."],
    offset: Annotated[int, "First line to return, 1-based."] = 1,
    limit: Annotated[int, "Maximum number of lines (max 5000)."] = 2000,
    pages: Annotated[str | None, "PDF only: page range like '1-5' (max 20 pages)."] = None,
) -> ToolResult:
    """Read a text file with line numbers; images and PDFs are supported where possible."""
```

| Input | Rules |
|---|---|
| `path` | non-empty; outside the root needs approval |
| `offset` | ≥ 1 |
| `limit` | 1–5000 |
| `pages` | only for `.pdf`; `N` or `N-M`, at most 20 pages |

**Behaviour**

1. Resolve the path. Missing → `not_found` with `hint: did you mean <up to 3 closest names in that folder>` (difflib). A directory → `invalid_args` with `hint: use list_dir`.
2. Images (`.png .jpg .jpeg .gif .webp`): if the role's model has `vision`, downscale to max 1568 px on the long side and return it in `images` with text `image: <path> (<w>x<h>, <size>)`; otherwise `unsupported`.
3. PDFs: run `pdftotext -layout -f N -l M <file> -` if on `PATH`; else `unsupported` with `hint: install poppler-utils`. PDFs over 10 pages require `pages`.
4. Notebooks (`.ipynb`): render each cell as `--- cell <i> [code|markdown] id=<cell_id> ---` followed by its source and, for code, `--- output ---` with text outputs (images listed as `[image output]`).
5. Other binary files → `binary_file`.
6. Text: split into lines; `offset` past the end → `invalid_args` (`offset 900 is past the end; file has 142 lines`). Take lines `offset..offset+limit-1`; cut any line over 2,000 characters to 2,000 plus ` [line truncated]`. Stop early if the output would exceed 30,000 characters.
7. Record the ledger entry; `full=True` only when every line of the file was returned.

**Output:**

```
file: src/app.py (142 lines, 4.1 KB)
     1	import os
     2	
     3	def main():
```

When not all lines were returned, the last line is `[PARTIAL: lines 1-2000 of 5230. Continue with read_file("src/app.py", offset=2001).]`. An empty file returns `file: src/empty.py (empty)`.

**Errors:** `invalid_args`, `not_found`, `permission_denied`, `binary_file`, `unsupported`, `too_large` (files over 50 MB).

**Tests:** line-number format; offset/limit and the PARTIAL line; long line cut; empty file; CRLF file reads without `\r`; Latin-1 note; image returns `images` for a vision model and `unsupported` otherwise; ledger `full` flag.

### `write_file`

```python
@tool(group="files", permission="auto", read_only=False, specifier_arg="path")
async def write_file(
    ctx: Ctx,
    path: Annotated[str, "File to create or overwrite."],
    content: Annotated[str, "The complete new file content."],
) -> ToolResult:
    """Create a new file or replace an existing file completely. Prefer edit_file for partial changes."""
```

| Input | Rules |
|---|---|
| `path` | inside the root or a writable root; not protected |
| `content` | max 5 MB (UTF-8 encoded) |

**Behaviour**

1. Resolve; check `outside_root` and `protected_path`.
2. If the file exists: it must have a ledger entry with `full=True` (else `not_read`), and its current sha256 must equal the ledger's (else `stale`, `hint: read the file again`).
3. Convert `content` to the existing file's line endings and BOM (new files: `\n`, no BOM). Ensure a trailing newline if the old file had one.
4. `apply_changes`; create parent folders as needed.

**Output:** new file `created src/util.py (24 lines)`; existing file `overwrote src/app.py (142 -> 150 lines, +12 -4)`.

**Errors:** `invalid_args`, `outside_root`, `protected_path`, `not_read`, `stale`, `too_large`, `permission_denied`, `sandbox_denied`.

**Tests:** creates parents; overwrite without read fails `not_read`; overwrite after external change fails `stale`; CRLF file stays CRLF; `.git/config` refused; diff counts correct.

### `edit_file`

```python
@tool(group="files", permission="auto", read_only=False, specifier_arg="path")
async def edit_file(
    ctx: Ctx,
    path: Annotated[str, "File to edit."],
    old: Annotated[str, "Exact text to replace, including whitespace and indentation."],
    new: Annotated[str, "Replacement text (may be empty to delete)."],
    replace_all: Annotated[bool, "Replace every occurrence instead of exactly one."] = False,
) -> ToolResult:
    """Replace an exact, unique piece of text in a file."""
```

| Input | Rules |
|---|---|
| `path` | must exist; inside the root; not protected |
| `old` | non-empty |
| `new` | must differ from `old` |

**Behaviour**

1. Resolve. Missing → `not_found` with `hint: use write_file to create a new file`.
2. Require a ledger entry (partial reads are enough), else `not_read`.
3. Read the current text. If the file uses `\r\n`, convert `\n` in `old` and `new` to `\r\n`.
4. Count occurrences of `old` (plain substring, no regex). 0 → `no_match` with `hint:` the 3 most similar lines with their line numbers (difflib ratio on the first line of `old`). More than 1 and not `replace_all` → `not_unique` with `hint: found at lines 12, 48, 90; add surrounding lines to make it unique or set replace_all`.
5. If the sha256 differs from the ledger, continue (the match was exact against current content) but add the note line below.
6. Replace; `apply_changes`.

**Output:**

```
edited src/app.py: 1 replacement at line 40
    37	def main():
    38	    cfg = load()
    39	
    40	    print("hello")
    41	    return 0
    42	
    43	if __name__ == "__main__":
```

Shows 3 lines of context around each replacement, at most 3 snippets, then `[+ 5 more replacements]` if any. If the file had changed on disk: `note: file had changed on disk since your last read; re-read before larger edits`.

**Errors:** `invalid_args`, `not_found`, `not_read`, `no_match`, `not_unique`, `outside_root`, `protected_path`, `binary_file`, `permission_denied`.

**Tests:** single replace; `not_unique` lists line numbers; `replace_all`; `no_match` hint shows nearest lines; empty `new` deletes; CRLF file edited with LF input; edit after external change still works when unique.

### `apply_patch`

```python
@tool(group="files", permission="auto", read_only=False)
async def apply_patch(
    ctx: Ctx,
    patch: Annotated[str, "Patch in the Forge/Codex format, from *** Begin Patch to *** End Patch."],
) -> ToolResult:
    """Add, update, move and delete several files in one atomic change."""
```

**Format** (parsed by `runtime/patch.py`):

```
*** Begin Patch
*** Add File: <path>
+<line>                      (every line of the new file starts with +)
*** Delete File: <path>
*** Update File: <path>
*** Move to: <new path>      (optional, directly after Update File)
@@ <optional context header, e.g. a def or class line>
 <context line>              (space prefix)
-<removed line>
+<added line>
*** End of File              (optional: hunk must touch the file end)
*** End Patch
```

**Behaviour**

1. Parse strictly; any unknown line → `invalid_args` with `hint: line <n>: <what was expected>`. Max patch size 2 MB.
2. For every file: Add requires the path not to exist; Update and Delete require it to exist and to be in the ledger (`not_read`); all paths pass the `outside_root` and `protected_path` checks. A permission check runs per path with specifier = path.
3. Apply hunks in memory, in order. A hunk's old text is its context and `-` lines. Search from the end of the previous hunk (or from the first line matching the `@@` header): exact match first, then a match ignoring trailing whitespace, then ignoring all leading and trailing whitespace. Zero matches → `no_match`; several at the same level → `not_unique`. The error names the file and hunk number and shows the expected lines.
4. Only if every file succeeds: one `apply_changes` call with all changes. Otherwise nothing is written.

**Output:**

```
applied patch: 3 files
  M src/app.py (+4 -2)
  A src/util.py (+24)
  R src/old.py -> src/new.py (+1 -1)
  D legacy/x.py
```

**Errors:** `invalid_args`, `not_found`, `not_read`, `no_match`, `not_unique`, `outside_root`, `protected_path`, `too_large`, `permission_denied`.

**Tests:** add, update, delete and move in one patch; second hunk failing leaves all files untouched; whitespace-tolerant matching; `@@` header narrows the search; Add on an existing file fails; parse error names the line.

### `list_dir`

```python
@tool(group="files", permission="auto", read_only=True, specifier_arg="path")
async def list_dir(
    ctx: Ctx,
    path: Annotated[str, "Folder to list."] = ".",
    depth: Annotated[int, "How many levels deep (1-5)."] = 2,
    show_hidden: Annotated[bool, "Include dotfiles and ignored files."] = False,
) -> ToolResult:
    """Show a folder as a tree, skipping ignored files."""
```

**Behaviour:** collect entries with `runtime/ignore.py` (in a git repo: `git ls-files --cached --others --exclude-standard`; otherwise the built-in list `.git node_modules .venv venv __pycache__ dist build .forge .mypy_cache .pytest_cache`); folders first, then files, both alphabetical (case-insensitive); stop at 500 entries.

**Output:**

```
src/ (2 levels)
├── forge/
│   ├── agent.py  4.2 KB
│   └── tools.py  38.0 KB
└── main.py  312 B
[cut: 37 more entries; use a deeper path or a lower depth]
```

**Errors:** `invalid_args` (not a folder, depth out of range), `not_found`, `permission_denied`.

**Tests:** gitignored files hidden unless `show_hidden`; depth respected; cut line at 500 entries; folders sorted before files.

## Search

`glob`, `grep` and `list_dir` share `runtime/ignore.py`, so the model sees the same set of files everywhere. Files over 2 MB and binary files are skipped by `grep` and `repo_map`.

### `glob`

```python
@tool(group="search", permission="auto", read_only=True, specifier_arg="path")
async def glob(
    ctx: Ctx,
    pattern: Annotated[str, "Glob like '**/*.py' or 'src/**/*.{ts,tsx}'."],
    path: Annotated[str, "Folder to search in."] = ".",
    limit: Annotated[int, "Maximum results (max 1000)."] = 100,
    include_ignored: Annotated[bool, "Also match gitignored files."] = False,
) -> ToolResult:
    """Find files by name pattern, newest first."""
```

**Behaviour:** expand braces first (`{a,b}` → two patterns; nested braces allowed); match each pattern with `pathlib.Path.glob` relative to `path`; keep files only; drop ignored files unless `include_ignored`; sort by modification time, newest first; keep `limit`.

**Output:** one root-relative path per line. If cut: last line `[truncated: 340 matches, showing the 100 newest]`. No match (ok=True): `no files match "<pattern>" in <path>`.

**Errors:** `invalid_args` (empty pattern, pattern containing a NUL byte, `limit` out of range), `not_found` (path), `permission_denied`.

**Tests:** `**/*.py` recursive; braces; newest first; ignored files excluded by default; truncation line.

### `grep`

```python
@tool(group="search", permission="auto", read_only=True, specifier_arg="path")
async def grep(
    ctx: Ctx,
    pattern: Annotated[str, "Regular expression (ripgrep / Rust regex syntax)."],
    path: Annotated[str, "File or folder to search."] = ".",
    glob: Annotated[str | None, "Only files matching this glob, e.g. '*.tsx'."] = None,
    type: Annotated[str | None, "Only files of this type, e.g. 'py', 'js', 'rust'."] = None,
    mode: Annotated[Literal["files", "content", "count"], "What to return."] = "files",
    context: Annotated[int, "Lines of context around matches in content mode (0-10)."] = 0,
    case_insensitive: Annotated[bool, "Ignore case."] = False,
    multiline: Annotated[bool, "Let patterns span lines."] = False,
    head_limit: Annotated[int, "Maximum result lines (max 2000)."] = 200,
    offset: Annotated[int, "Skip this many result lines (for paging)."] = 0,
) -> ToolResult:
    """Search file contents with a regular expression."""
```

**Behaviour**

1. If `rg` is on `PATH`, run `rg --json --max-filesize 2M` plus: `-i` for `case_insensitive`, `-U --multiline-dotall` for `multiline`, `-g <glob>`, `-t <type>`, `-C <context>` in content mode. Parse the JSON events.
2. Otherwise, the Python fallback: files from `runtime/ignore.py`, filtered by `glob` and by a built-in type map (`py: *.py *.pyi`, `js: *.js *.mjs *.cjs`, `ts: *.ts *.tsx`, …); compile with `re` (`re.I`, `re.M | re.S` for multiline); search line by line.
3. Sort files by modification time, newest first. Apply `offset` and `head_limit` to the result lines.

**Output by mode** (ripgrep conventions):

```
files:    src/auth.py
          src/api/login.py
content:  src/auth.py:41:def check_token(token: str) -> bool:
          src/auth.py-42-    if not token:         (context line: '-' instead of ':')
          --                                       (between separate groups)
count:    src/auth.py:3
          src/api/login.py:1
          total: 4 matches in 2 files
```

If cut: last line `[showing 200 of 1,240 results; use offset=200 for more]`. No match (ok=True): `no matches for /<pattern>/ in <path>`. In count mode, `total` always covers all matches, even when the per-file lines are cut.

**Errors:** `invalid_args` (bad regex: the regex engine's message is included; context out of range), `not_found`, `permission_denied`.

**Tests:** each mode; context lines and separators; gitignored files skipped; invalid regex message; paging with offset; the Python fallback gives the same output as rg on a fixture tree.

### `repo_map`

```python
@tool(group="search", permission="auto", read_only=True, specifier_arg="path")
async def repo_map(
    ctx: Ctx,
    path: Annotated[str, "Folder to map."] = ".",
    max_tokens: Annotated[int, "Size budget for the map (200-20000)."] = 2000,
    include_private: Annotated[bool, "Include names starting with '_'."] = False,
) -> ToolResult:
    """Outline of the most important files with their classes, functions and signatures."""
```

**Behaviour**

1. List source files under `path` via `runtime/ignore.py`; map extensions to tree-sitter languages (`tree-sitter-language-pack`). Other files are counted but not parsed.
2. Parse each file, using the cache `.forge/cache/repomap.json` keyed by path + mtime. Extract definitions: classes, functions, methods, and top-level constants in UPPER_CASE, each with its signature line (first line of the definition, trimmed to 120 chars).
3. Rank: count, across all files, how often each defined name appears as an identifier in *other* files; a file's score is the sum over its definitions, plus 1. Sort by score, then by path.
4. Emit files in rank order until the budget (characters / 4) is used.

**Output:**

```
src/forge/agent.py
│ class AgentResult(BaseModel)
│ async def run_agent(ctx: Ctx, task: str, *, role: str = "coder", ...) -> AgentResult
src/forge/tools.py
│ def tool(*, group: str, permission: Permission = "auto", ...) -> Callable
│ async def call_tool(ctx: Ctx, call: ToolCall) -> ToolResult
[repo_map: 48 of 112 files shown within 2000 tokens; 9 files in unsupported languages]
```

**Errors:** `invalid_args`, `not_found`.

**Tests:** a fixture repo where the most referenced file comes first; budget respected; private names hidden by default; cache reused on the second call (parse count stays 0).

## Web

Both tools run inside the Forge process (`runtime/web.py`, using `httpx`), not in the command sandbox, so only their permission rules control them.

### `web_fetch`

```python
@tool(group="web", permission="ask", read_only=True, specifier_arg="url")
async def web_fetch(
    ctx: Ctx,
    url: Annotated[str, "Full http(s) URL."],
    question: Annotated[str | None, "If set, return only the answer to this question, extracted from the page."] = None,
    max_chars: Annotated[int, "Maximum characters of page content to return (1000-100000)."] = 20000,
) -> ToolResult:
    """Download a web page and return it as Markdown, or answer a question about it."""
```

**Permission specifier** is `domain:<host>`, so a rule `web_fetch(domain:docs.python.org)` allows that host; approving once with "remember" adds such a rule for the session.

**Behaviour**

1. Parse the URL; only `http` and `https`; upgrade `http` to `https`.
2. Refuse local targets: hosts without a dot, `localhost`, and any host whose DNS answers include a private, loopback, link-local or cloud-metadata address (`169.254.169.254`) → `invalid_args` with `hint: use bash with curl for local servers`.
3. Cache: return a cached copy if fetched in the last 15 minutes (`.forge/cache/web/<sha256(url)>.json`).
4. GET with a 10 s connect and 300 s total timeout, `User-Agent: Forge/<version>`, `Accept: text/markdown, text/html;q=0.9, */*;q=0.5`. Follow redirects on the same host (max 5). A redirect to another host is not followed: return ok=True with `redirected to <url>; call web_fetch again with that URL if you trust it`.
5. Stop reading after 10 MB → `too_large`. 4xx/5xx → `http_status` with the status line.
6. Convert by content type: HTML → remove `script style nav footer header noscript iframe`, convert with `markdownify` (ATX headings, links kept); Markdown, plain text, JSON, XML → unchanged; PDF and other binary → `unsupported`.
7. Cut to `max_chars`.
8. If `question` is set: call the `compressor` role's model with the `WEB_EXTRACT` prompt from `prompts.py` and the content; return its answer instead of the page. The page content is passed as data, and the prompt tells the model to ignore instructions found inside it.

**Output:**

```
url: https://docs.python.org/3/library/asyncio.html (200, text/html, 48.2 KB -> 12,004 chars)
title: asyncio — Asynchronous I/O
--- content ---
<markdown>
```

With `question`, the last section is `--- answer ---` followed by the answer and up to 3 short supporting quotes. A cut page ends with `[content cut at 20000 chars]`. Cached results add `(cached)` to the first line.

**Errors:** `invalid_args`, `permission_denied`, `http_status`, `network`, `timeout`, `too_large`, `unsupported`.

**Tests (respx):** HTML converted to Markdown without scripts; localhost and `10.0.0.1` refused; cross-host redirect reported, not followed; cache hit on second call; 404 gives `http_status`; `question` path calls the fake model with the page as data.

### `web_search`

```python
@tool(group="web", permission="ask", read_only=True)
async def web_search(
    ctx: Ctx,
    query: Annotated[str, "Search query."],
    allowed_domains: Annotated[list[str] | None, "Only return results from these domains."] = None,
    blocked_domains: Annotated[list[str] | None, "Never return results from these domains."] = None,
    max_results: Annotated[int, "Number of results (1-20)."] = 8,
) -> ToolResult:
    """Search the web and return titles, URLs and snippets. Use web_fetch to read a result."""
```

| Input | Rules |
|---|---|
| `query` | 1–400 chars |
| `allowed_domains`, `blocked_domains` | bare hosts like `python.org`; not both at once |
| `max_results` | 1–20 |

**Behaviour**

1. Count searches per session; over 200 → `limit_reached` with `hint: continue with the information you have`.
2. Backend from `[web] search_backend`: `native` uses the current coder model's provider search tool when `Capabilities.web_search` is true (one extra provider call that returns results only); otherwise, or with `brave`, `tavily` or `searxng`, call that HTTP API with the key from `search_api_key_env`. No backend available → `unsupported` with `hint: set [web] search_backend in forge.toml`.
3. Apply domain filters (passed to the backend where supported, and always filtered again afterwards). Drop duplicate URLs.

**Output:**

```
results for "asyncio taskgroup cancel" (backend: brave, 8 results)
1. asyncio — Task groups
   https://docs.python.org/3/library/asyncio-task.html
   Task groups combine a task creation API with a convenient ...
2. ...
```

No results (ok=True): `no results for "<query>"`.

**Errors:** `invalid_args`, `permission_denied`, `unsupported`, `limit_reached`, `http_status`, `network`.

**Tests:** each backend with a recorded response; both filter lists at once refused; blocked domain removed even if the backend returns it; the 201st search hits `limit_reached`.


### `research`

```python
@tool(group="web", permission="auto", read_only=True)
async def research(
    ctx: Ctx,
    question: Annotated[str, "The question with everything the researcher needs ..."],
    browser: Annotated[bool, "Use a real browser with screenshots ..."] = False,
    depth: Annotated[Literal["normal", "deep"], "normal: one focused question; deep: ..."] = "normal",
) -> str:
```

- **Inputs:** `question` 1-20,000 characters; `depth` sets the researcher's turn limit (normal 15, deep 40).
- **Behaviour:** main agent only (also in solo mode, where `spawn_agent` is hidden). Starts a `researcher` sub-agent in the foreground with the RESEARCHER prompt and the read-only tools (`web_search`, `web_fetch`, file reading); its approvals go to the user as usual. `browser=true` starts the `browser` agent instead; it needs a browser and a browser-role model with vision, otherwise `unsupported`. Limits of `spawn_agent` apply (`max_parallel_agents`, budget).
- **Output:** as `spawn_agent`: `agent <id> (researcher) finished: <status>, <n> turns, ...`, then `--- report ---` and the report.
- **Errors:** `unsupported` (sub-agent caller; browser not available), `limit_reached`, `invalid_args`.
- **Tests:** report returned; visible in solo mode, hidden from sub-agents; depth sets the turn limit; prompts tell the coder and lead to prefer it.

**Search fallback.** With `search_backend = "native"` and a model without its own search tool, `web_search` uses `web.fallback_backend` (with the key from `search_api_key_env`) when both are set; the native search uses the calling agent's model.
## Browser

Only the `browser` role gets these tools; `research(browser=true)` starts that role. Each agent has its own browser context, which is closed when the agent finishes. Every action waits for the page to settle and then returns `title: ...`, `url: ...` and a JPEG screenshot of the viewport (`[browser] viewport_width` x `viewport_height`). After `[browser] max_screenshots` screenshots per agent, only the text is returned. The browser aborts every request to local or private hosts, and `browser_open` checks its URL like `web_fetch`.

| Tool | Permission | Arguments | Does |
|---|---|---|---|
| `browser_open` | ask (specifier: url) | `url` | load a page |
| `browser_click` | auto | `target`: visible text, `css=<selector>` or `x,y` | click, then show the page |
| `browser_type` | auto | `target` (label, placeholder or `css=`), `text`, `submit=false` | fill a field, Enter if `submit` |
| `browser_scroll` | auto | `pixels=700` (negative: up) | scroll |
| `browser_back` | auto | none | go back |
| `browser_screenshot` | auto | none | show the page again |
| `browser_read` | auto | none | the visible text (max 20,000 chars), no screenshot |

- **Errors:** `unsupported` (no browser or Chromium is missing; the hint names `forge browser install`), `invalid_args` (local URL), `not_found` (element not found), `network` (navigation failed or timed out).
- **Tests:** only the browser role sees the tools; a screenshot is in every action; local URLs are refused; one browser per agent, closed at the end; the screenshot limit applies; real-Chromium conformance in `tests/conformance/test_browser_conformance.py`.

## Plan and interaction

These four tools connect the agent loop to the pipeline. They read and write `ctx.session.spec` and `ctx.session.plan`, save through `ctx.store` after every change, and publish `QuestionAsked` / `PlanUpdated` / `StepDone` events.

### `ask_user`

```python
class QuestionIn(BaseModel):
    text: str = Field(max_length=300)
    kind: Literal["choice", "multi", "text", "confirm"]
    options: list[str] = []          # 2-6 for choice/multi, each <= 80 chars, unique
    default: str | None = None       # choice: one option; multi: options joined by ", "; confirm: "yes"/"no"
    why: str = Field(max_length=120)

@tool(group="plan", permission="auto", read_only=True)
async def ask_user(
    ctx: Ctx,
    questions: Annotated[list[QuestionIn], "1-4 questions, most important first."],
) -> ToolResult:
    """Ask the user questions whose answers change the result. Never ask what you can find in the repo."""
```

**Behaviour**

1. Only `ctx.agent_id == "main"` may call it; others → `unsupported` with `hint: report the question to the lead with send_message`.
2. Validate: 1–4 questions; `options` count fits `kind`; `default` valid for `kind`. Convert to `plan.Question` and publish `QuestionAsked`.
3. Headless (`ctx.headless`): each answer is its `default`; without a default, choice takes the first option, confirm takes "no", text and multi take "" — each such answer is appended to `spec.assumptions` (or the session notes if no spec yet) as `Assumed for "<text>": <answer>`.
4. Interactive: `await ctx.renderer.ask(questions)`. Choice and multi also accept free text through an "Other" row. No timeout.

**Output:**

```
1. Which database should the cache use?
   answer: Redis
2. Should cached entries expire?
   answer: yes (default, headless)
```

**Errors:** `invalid_args`, `unsupported`, `cancelled` (user pressed Esc: `error[cancelled]: user dismissed the questions` and `hint: continue with your best judgment and state your assumptions`).

**Tests:** answers come back in order; headless uses defaults and records assumptions; a sub-agent call returns `unsupported`; 5 questions refused; a choice default not in the options refused.

### `submit_plan`

```python
class StepIn(BaseModel):
    title: str = Field(max_length=100)
    detail: str
    files: list[str] = []
    depends_on: list[int] = []       # 1-based numbers of earlier steps in this list
    check: str                       # shell command, or "review: <criterion>"
    role: str = "coder"

@tool(group="plan", permission="auto", read_only=True)
async def submit_plan(
    ctx: Ctx,
    steps: Annotated[list[StepIn], "Ordered steps, 1-30."],
    explanation: Annotated[str, "One or two sentences on the approach."] = "",
) -> ToolResult:
    """Submit the implementation plan for user approval."""
```

**Behaviour**

1. Only for the `planner` role or plan mode; otherwise `unsupported`. Requires `ctx.session.spec` (else `invalid_args`).
2. Build `Step`s with ids `s1..sN`; map `depends_on` numbers to ids. Run `Plan.validate_graph()`; also check each `role` exists and every `files` path is inside the root. Any error → `invalid_args` with one `- ` line per problem.
3. Ask for approval: interactive → `renderer.approve` with the plan; headless → approved. If the plan already exists, the new one gets `version + 1`.
4. Approved → save to `session.plan`, publish `PlanUpdated`. Rejected → `permission_denied` with the user's feedback as `hint:` so the planner can revise.

**Output:**

```
plan approved: 6 steps (version 1)
s1 [coder]  Add Token model                -> check: pytest tests/test_token.py
s2 [coder]  Add JWT middleware (after s1)  -> check: pytest tests/test_auth.py
s3 [tester] Add login tests (after s2)     -> check: review: covers expired tokens
```

**Errors:** `invalid_args`, `unsupported`, `permission_denied`.

**Tests:** valid plan saved with ids; a cycle reported; unknown role reported; rejection returns feedback; resubmission bumps the version.

### `update_plan`

```python
class StepUpdate(BaseModel):
    step_id: str
    status: Literal["todo", "doing", "failed", "skipped"]   # "done" is not allowed here
    note: str = ""

@tool(group="plan", permission="auto", read_only=True)
async def update_plan(
    ctx: Ctx,
    updates: Annotated[list[StepUpdate], "Status changes, 1-30."],
    explanation: Annotated[str, "Short reason, max 200 chars."] = "",
) -> ToolResult:
    """Change step statuses. Use finish_step to mark a step done."""
```

**Behaviour**

1. Requires a plan (`invalid_args` otherwise). Every `step_id` must exist.
2. Allowed transitions: `todo -> doing | skipped`, `doing -> todo | failed | skipped`, `failed -> todo`. Anything else → `invalid_args` naming the step and transition. A status of `done` is rejected by the schema with `hint: use finish_step`.
3. `skipped` requires a non-empty `note`. After applying, at most one step may be `doing`.
4. Apply all or nothing; save; publish `PlanUpdated`.

**Output** (the TUI renders the plan; the prompt tells the model not to repeat it):

```
plan updated (version 1)
[x] s1 Add Token model
[>] s2 Add JWT middleware
[ ] s3 Add login tests
[-] s4 Update docs (skipped: no docs folder)
[!] s5 Migrate DB (failed)
```

**Errors:** `invalid_args`.

**Tests:** valid transitions; `done` refused with the hint; two `doing` refused; skip without note refused; failed batch changes nothing.

### `finish_step`

```python
@tool(group="plan", permission="auto", read_only=False)
async def finish_step(
    ctx: Ctx,
    step_id: Annotated[str, "The step you completed, e.g. 's3'."],
    summary: Annotated[str, "What you changed, max 2000 chars."],
    evidence: Annotated[str, "Commands you ran and their results, max 4000 chars."] = "",
) -> ToolResult:
    """Report a step as complete; Forge runs its check and only then marks it done."""
```

**Behaviour**

1. The step must exist and be `doing` (else `invalid_args`).
2. `pipeline.verify_step(ctx, step)`: if `check` starts with `review:`, the `reviewer` role judges the diff since the step's checkpoint against the criterion and returns JSON `{"pass": bool, "reason": str}`; otherwise the check runs as a command through `ctx.executor` (bash on POSIX, PowerShell on Windows, sandboxed, timeout 600 s), and exit code 0 means pass.
3. Pass → `status="done"`, `notes=summary`; publish `StepDone(ok=True)`; run `step_done` hooks; save.
4. Fail → `attempts += 1`. If `attempts < limits.max_step_attempts`: stay `doing`, return `check_failed` with the check output. Otherwise set `failed`, return `limit_reached`; the pipeline then calls the replanner.

**Output (pass):**

```
step s3 done: check passed (pytest tests/test_auth.py, exit 0, 2.1s)
next step: s4 Add login endpoint
```

**Output (fail):**

```
error[check_failed]: step s3 check failed (attempt 2 of 3)
--- check output ---
FAILED tests/test_auth.py::test_expired_token - AssertionError ...
```

**Errors:** `invalid_args`, `check_failed`, `limit_reached`, `timeout` (check took over 600 s).

**Tests:** passing check marks done; failing check keeps `doing` and returns output; third failure sets `failed`; `review:` check uses the fake reviewer; a step not in `doing` refused.

## Agents

`team.py` keeps an `AgentRegistry` per session: `AgentInfo(id, name, role, status, parent_id, task, turns, usage, worktree, inbox: asyncio.Queue[str], task_handle)`. Ids are `a1`, `a2`, … in creation order; `main` is the top-level agent. Statuses: `running`, `done`, `stopped`, `failed`, `cancelled`.

### `spawn_agent`

```python
@tool(group="agents", permission="auto", read_only=False, specifier_arg="role")
async def spawn_agent(
    ctx: Ctx,
    role: Annotated[str, "Role name: explore, coder, tester, reviewer, researcher, or a custom agent."],
    task: Annotated[str, "Self-contained instructions: goal, relevant files, what to return."],
    background: Annotated[bool, "Run in parallel and get a message when done."] = False,
    isolation: Annotated[Literal["none", "worktree"], "Run in its own git worktree."] = "none",
    max_turns: Annotated[int, "Turn limit for the sub-agent (1-200)."] = 30,
    name: Annotated[str | None, "Optional name for messaging, e.g. 'api-tests'."] = None,
) -> ToolResult:
    """Start a sub-agent with its own context. It returns only its final report."""
```

| Input | Rules |
|---|---|
| `role` | known built-in or custom role |
| `task` | 1–20,000 chars |
| `max_turns` | 1–200 |
| `name` | `^[a-z0-9-]{1,32}$`, unique in the session |

**Behaviour**

1. Only `main` (the lead) may spawn; others → `unsupported`. Check `max_parallel_agents` (running agents) and the session cost budget → `limit_reached`.
2. Build a child `Ctx`: new `agent_id`, `role`, same session, store, bus and renderer; tools = `for_role(role)` minus `ask_user`, `spawn_agent`, `submit_plan`; sandbox = the stricter of the parent's and the role's config; `cwd` = the parent's cwd.
3. `isolation="worktree"`: `runtime/worktree.py` creates `.forge/worktrees/<agent_id>` on branch `forge/<session_id>/<agent_id>` from the current HEAD plus uncommitted changes; the child's `root` and `cwd` point there.
4. Foreground: `await run_agent(child, task, role=role, max_turns=max_turns)`; run `subagent_stop` hooks; mark status. Background: start it as an asyncio task and return at once; on completion, put `[agent a3 (tester) finished: <status>]\n<final text>` into the parent's inbox and publish an event.
5. The child's transcript is saved in the session store under its id; the parent receives only the final text.

**Output (foreground):**

```
agent a2 (explore) finished: done, 14 turns, 38.2k tokens, $0.04
--- report ---
<final text of the sub-agent>
```

Stopped by the turn limit: first line `agent a2 (explore) stopped: max_turns (partial report)`. Background start: `started agent a3 (tester) in background; you will receive its report as a message`.

**Errors:** `invalid_args`, `unsupported`, `limit_reached`, `not_found` (unknown role), `permission_denied` (a rule like `spawn_agent(researcher)` in `deny`).

**Tests:** the parent sees only the report; the child cannot call `ask_user`; a reviewer child cannot edit; a worktree child's edits do not touch the main tree; background completion arrives in the inbox; `max_parallel_agents` enforced.

### `send_message`

```python
@tool(group="agents", permission="auto", read_only=True)
async def send_message(
    ctx: Ctx,
    to: Annotated[str, "Agent id or name, or 'main' for the lead."],
    text: Annotated[str, "The message, max 10,000 chars."],
    summary: Annotated[str, "Optional one-line preview, max 100 chars."] = "",
) -> ToolResult:
    """Send a message to another running agent."""
```

**Behaviour:** resolve `to` by id or name; the target must be `running` (else `not_found` with `hint: agent a2 has finished`); put `[message from <id> (<role>)]: <text>` into its inbox. The agent loop drains the inbox at the start of every turn and appends each message as a user message. The lead's renderer shows every message with its summary.

**Output:** `delivered to a3 (tester)`.

**Errors:** `invalid_args` (empty text, sending to yourself), `not_found`.

**Tests:** message arrives at the target's next turn; finished target refused; name and id both resolve.

### `list_agents`

```python
@tool(group="agents", permission="auto", read_only=True)
async def list_agents(ctx: Ctx) -> ToolResult:
    """List all agents in this session with status and usage."""
```

**Output:**

```
id    name       role      status    turns  tokens  task
main  -          coder     running   22     81.4k   Add JWT authentication
a1    -          explore   done      14     38.2k   Find where tokens are validated
a3    api-tests  tester    running   6      12.0k   Write tests for /login
```

The `task` column shows the first 50 characters.

**Errors:** none.

**Tests:** columns and order; statuses update after completion.

### `stop_agent`

```python
@tool(group="agents", permission="auto", read_only=False)
async def stop_agent(
    ctx: Ctx,
    agent_id: Annotated[str, "Agent id or name."],
    keep_worktree: Annotated[bool, "Keep its worktree for inspection."] = True,
) -> ToolResult:
    """Cancel a running agent and its background jobs."""
```

**Behaviour:** only `main` may stop agents; cancel the asyncio task; stop the agent's background jobs (as `job_stop`); release its claimed board tasks back to `todo`; set status `cancelled`; remove the worktree unless `keep_worktree`.

**Output:** `stopped a3 (tester) after 6 turns; worktree kept at .forge/worktrees/a3; released task s4`.

**Errors:** `not_found`, `unsupported` (caller is not `main`), `invalid_args` (target already finished; `hint: it ended with status done`).

**Tests:** a running agent stops within 1 s; its jobs stop; its claimed task goes back to `todo`.

## Team board

The board is the plan seen by a team: each `Step` is a task, with an extra `owner: str | None` stored next to it in the `Store` (`board` table: `session_id, step_id, owner, claimed_at`). Display status is computed: `done`, `failed`, `skipped` and `doing` come from the step; a `todo` step is `ready` when all dependencies are `done` or `skipped`, otherwise `blocked`. These three tools are given only to agents in team mode.

### `read_board`

```python
@tool(group="agents", permission="auto", read_only=True)
async def read_board(
    ctx: Ctx,
    status: Annotated[list[Literal["ready", "blocked", "doing", "done", "failed", "skipped"]] | None,
                      "Only these statuses; default all."] = None,
) -> ToolResult:
    """Show the team's tasks with status, owner and dependencies."""
```

**Output:**

```
id  status   owner  after   title
s1  done     a1     -       Add Token model
s2  doing    a2     s1      Add JWT middleware
s3  ready    -      s1      Add token refresh endpoint
s4  blocked  -      s2,s3   Add login tests
```

**Errors:** `invalid_args` (no plan yet).

**Tests:** ready vs blocked computed from dependencies; filter works.

### `claim_task`

```python
@tool(group="agents", permission="auto", read_only=False)
async def claim_task(ctx: Ctx, task_id: Annotated[str, "Step id, e.g. 's3'."]) -> ToolResult:
    """Take a ready task so no other agent works on it."""
```

**Behaviour:** in one store transaction: the step exists (`not_found`), is `ready` (`invalid_args` with `hint: waiting for s2` or `hint: already done`), and has no owner (`busy` with `hint: owned by a2`); then set `owner = ctx.agent_id`, step status `doing`, save, publish `PlanUpdated`. An agent may own one task at a time (`invalid_args` with `hint: finish s2 first`).

**Output:**

```
claimed s3: Add token refresh endpoint
detail: <step detail>
files: src/api/auth.py, tests/test_refresh.py
check: pytest tests/test_refresh.py
```

**Errors:** `not_found`, `invalid_args`, `busy`.

**Tests:** 4 agents race for 8 tasks 50 times with no double claim; blocked task refused; second claim while owning one refused.

### `update_task`

```python
@tool(group="agents", permission="auto", read_only=False)
async def update_task(
    ctx: Ctx,
    task_id: Annotated[str, "Step id you own."],
    status: Annotated[Literal["doing", "done", "failed"], "New status."],
    result: Annotated[str, "What you did, or why it failed (max 4000 chars)."],
) -> ToolResult:
    """Report progress or the result of your task to the lead."""
```

**Behaviour**

1. The caller must own the task (`permission_denied` with `hint: owned by a2`).
2. `doing`: store `result` as a progress note; send it to the lead's inbox.
3. `done`: run exactly the same verification as `finish_step`. Pass → step `done`, owner kept for history, lead receives `[task s3 done by a3] <result>` and merges the agent's branch (S34). Fail → `check_failed` with the output; the task stays with the caller.
4. `failed`: `result` required; step `failed`; owner cleared; lead notified and decides (retry, replan, or reassign).

**Output:** `s3 done: check passed; lead notified` — or for `failed`: `s3 marked failed; lead notified`.

**Errors:** `not_found`, `permission_denied`, `invalid_args`, `check_failed`.

**Tests:** non-owner refused; `done` with failing check stays `doing`; `failed` releases the owner and notifies the lead.

## Memory

**Memory files** are not a tool but are loaded by `memory.py` at session start, most specific last so it wins: `~/.forge/FORGE.md`, then for each folder from the repo root down to `cwd`: `FORGE.md`, `AGENTS.md`, `CLAUDE.md`. Each file is capped at 32 KB and inserted into the system prompt as `<memory path="<path>" scope="<folder>">...</memory>`. A file's instructions apply to its own folder tree; deeper files win on conflict; direct user instructions beat all files.

### `remember`

```python
@tool(group="memory", permission="ask", read_only=False)
async def remember(
    ctx: Ctx,
    note: Annotated[str, "One fact or rule to keep for future sessions, max 500 chars."],
    scope: Annotated[Literal["project", "user"], "project = ./FORGE.md, user = ~/.forge/FORGE.md."] = "project",
) -> ToolResult:
    """Save a lasting note, e.g. a build command or a coding convention."""
```

**Behaviour**

1. Validate: 1–500 chars, no line breaks (replace them with spaces).
2. Always asks the user (it changes future behaviour); the approval shows the note and the file.
3. Target file: `<root>/FORGE.md` or `~/.forge/FORGE.md`; create it with `# FORGE.md` if missing.
4. Find or create the heading `## Notes from Forge` at the end; skip if an identical bullet exists (`ok=True`, `already remembered`); else append `- <note> (added YYYY-MM-DD)`.
5. Write via `apply_changes` (so `/undo` covers it).

**Output:** `remembered in FORGE.md: "<note>"`.

**Errors:** `invalid_args`, `permission_denied`.

**Tests:** creates the file and heading; appends under an existing heading; duplicate skipped; user decline returns `permission_denied`.

### `recall`

```python
@tool(group="memory", permission="auto", read_only=True)
async def recall(
    ctx: Ctx,
    query: Annotated[str, "Words to search for in past sessions of this project."],
    limit: Annotated[int, "Number of results (1-20)."] = 5,
) -> ToolResult:
    """Search earlier sessions of this project: messages, summaries and plans."""
```

**Behaviour:** `ctx.store.search(project_root, query, limit)` (SQLite FTS5 over message text, compaction summaries and step titles/notes, excluding the current session); each hit is cut to 400 characters centred on the match.

**Output:**

```
1. session 3f2a9c1e (2026-10-02) "Add JWT auth"
   ...the refresh token lives in an httpOnly cookie, decided because...
2. session 91b0d4aa (2026-09-28) "Fix login redirect"
   ...
```

No hits (ok=True): `no earlier sessions mention "<query>"`.

**Errors:** `invalid_args` (empty query, limit out of range).

**Tests:** finds text from a previous session; never returns the current session; ordering by relevance then date.

## MCP

`mcp_client.py` connects every `[mcp_servers.<name>]` at session start using the official `mcp` package: stdio servers (`command`, `env_keys`) or HTTP servers (`url`, `headers_env`). A server that fails to start is reported once as an `ErrorEvent` and skipped; the session continues.

### Dynamic tools `mcp__<server>__<tool>`

Each tool a server lists becomes a `ToolDef`:

| Field | Value |
|---|---|
| `name` | `mcp__<server>__<tool>`, with any character outside `[a-zA-Z0-9_-]` replaced by `_`, max 64 chars |
| `spec` | the server's description and `inputSchema`, passed through unchanged |
| `permission` | `ask`; `allow = ["mcp__github__*"]` lifts it for a whole server |
| `read_only` | `True` only if the server marks the tool `readOnlyHint` |
| `group` | `mcp` |

**Behaviour:** `call_tool` runs the usual stages (permission, hooks, audit); arguments are checked for the schema's `required` fields and basic JSON types, then sent with `session.call_tool(name, args)` and a 120 s timeout (`timeout`). Result content blocks are converted: text blocks joined by blank lines; image blocks to `images` (if the model has vision; otherwise `[image: <mime>, <size>]`); resource links listed as `resource: <uri>`. `isError: true` → `ok=False`, code `tool_error`, with the server's text after the error line.

**Output:** the converted text, unchanged otherwise (then capped as usual).

**Errors:** `invalid_args`, `permission_denied`, `timeout`, `tool_error`, `network` (server connection lost; one reconnect is attempted first).

**Tests (stub MCP server in `tests/fixtures/mcp_stub.py`):** a tool is registered with the right name and schema; it requires approval by default; `isError` becomes `tool_error`; an image block becomes `images`.

### `list_mcp_resources`

```python
@tool(group="mcp", permission="auto", read_only=True)
async def list_mcp_resources(
    ctx: Ctx,
    server: Annotated[str | None, "Only this server; default all."] = None,
) -> ToolResult:
    """List resources offered by connected MCP servers."""
```

**Output:** one line per resource, `<server>  <uri>  <name>  <mime type>`; none (ok=True): `no MCP resources available`.

**Errors:** `not_found` (unknown server).

### `read_mcp_resource`

```python
@tool(group="mcp", permission="auto", read_only=True)
async def read_mcp_resource(
    ctx: Ctx,
    server: Annotated[str, "Server name."],
    uri: Annotated[str, "Resource URI from list_mcp_resources."],
) -> ToolResult:
    """Read one resource from an MCP server."""
```

**Output:** first line `resource: <uri> (<mime type>, <size>)`, then the text content; binary content as `images` (images, if vision) or `[binary resource, <size>, <mime>]`.

**Errors:** `not_found`, `timeout`, `tool_error`.

**Tests:** list and read against the stub server; unknown URI gives `not_found`.

### `tool_search`

```python
@tool(group="mcp", permission="auto", read_only=True)
async def tool_search(
    ctx: Ctx,
    query: Annotated[str, "Words describing the tool you need, e.g. 'create github issue'."],
    limit: Annotated[int, "Maximum tools to load (1-10)."] = 5,
) -> ToolResult:
    """Find and load MCP tools that are not yet in your tool list."""
```

**Behaviour:** active only when more MCP tools are connected than `limits.mcp_defer_threshold` (default 40). In that mode, deferred tools appear in the system prompt as one line each (`name: first sentence of description`) without schemas. `tool_search` scores deferred tools by word overlap between `query` and name + description (name matches count double), takes the top `limit` with score > 0, and adds their full specs to this agent's tool list for the rest of the session.

**Output:**

```
loaded 3 tools:
- mcp__github__create_issue: Create a new issue in a repository
- mcp__github__add_labels: Add labels to an issue or pull request
- mcp__github__search_issues: Search issues and pull requests
```

Nothing matched (ok=True): `no deferred tools match "<query>"; available servers: github, linear`. Not in deferred mode: `unsupported` with `hint: all tools are already loaded`.

**Errors:** `invalid_args`, `unsupported`.

**Tests:** with 50 stub tools, deferred mode lists names only; a search loads the right tool, which is then callable; below the threshold the tool returns `unsupported`.

## Planned for later versions

| Tool | Purpose | Modeled on |
|---|---|---|
| `code_intel` | Definitions, references, type errors via a language server | Claude Code `LSP` |
| `notebook_edit` | Edit Jupyter cells by id | Claude Code `NotebookEdit` |
| `watch` | Tail a log or poll a command and wake the agent on matching lines | Claude Code `Monitor` |
| `use_skill` | Load a `SKILL.md` on demand into context | Claude Code `Skill` |
