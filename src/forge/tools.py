"""tools.py — every tool Forge can use, in one file.

Table of contents:
  FRAMEWORK  ToolDef, REGISTRY, tool(), make_tool_def(), for_role(), call_tool()
  FILES      read_file, write_file, edit_file, list_dir
  SEARCH     glob, grep
  SHELL      bash, powershell, job_output, job_stop
  PLAN       ask_user, submit_plan

Each tool is a plain async function `fn(ctx, **args)` with a decorator. The decorator
reads the signature and docstring and generates the JSON schema, so a tool is written
once and never described twice.
"""

import asyncio
import difflib
import inspect
import json
import logging
import re
import shlex
import time
import typing
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Any, Literal, TypeVar, get_args, get_origin

from pydantic import BaseModel, ConfigDict, Field, ValidationError, create_model

from forge.config import ForgeConfig
from forge.ctx import Ctx
from forge.events import PlanUpdated
from forge.plan import Plan, Question, Step, TaskSpec, checklist
from forge.ports import Command, CommandResult, JobNotFoundError, SandboxPolicy
from forge.providers.base import ProviderError, ToolCall, ToolResult, ToolSpec
from forge.providers.registry import resolve_role
from forge.questions import ask
from forge.runtime.edit import adapt_newlines, edit_summary, replace_text
from forge.runtime.errors import ToolError, failure
from forge.runtime.files import (
    FileChange,
    TextFile,
    apply_changes,
    check_writable,
    decode_text,
    display_path,
    encode_text,
    is_binary,
    is_within,
    read_bytes,
    resolve_path,
    split_lines,
    writable_roots,
)
from forge.runtime.ignore import expand_braces, glob_regex, matches_glob, project_files
from forge.runtime.ledger import sha256_of
from forge.runtime.readers import IMAGE_TYPES, read_image, read_notebook, read_pdf, read_text
from forge.runtime.search import GrepQuery, format_hits, search
from forge.runtime.shell import ShellKind, find_shell
from forge.runtime.tree import build_tree, render_tree

log = logging.getLogger(__name__)

# =====================================================================================
# FRAMEWORK
# =====================================================================================

Permission = Literal["auto", "ask"]
ToolFn = Callable[..., Awaitable[str | ToolResult]]
F = TypeVar("F", bound=ToolFn)

OUTPUT_CAP_OK = 30_000  # chars inline on success
OUTPUT_PREVIEW = 2_000  # chars shown when spilled
OUTPUT_CAP_FAIL = 10_000  # head+tail chars on failure

READ_ONLY_ROLES = frozenset({"reviewer", "explore", "researcher", "planner"})
_DATA_KEYS = frozenset({"default", "enum", "const", "examples"})


@dataclass(frozen=True)
class ToolDef:
    """A registered tool: its function, schema and permission settings."""

    name: str
    group: str  # "shell", "files", "search", "web", "plan", "agents", "memory", "mcp"
    fn: ToolFn
    spec: ToolSpec  # generated from signature + docstring
    permission: Permission
    read_only: bool  # True = allowed in plan mode and for reviewer roles
    specifier_arg: str | None  # arg used by rules: "command", "path", "url", "role"
    args_model: type[BaseModel] | None = None  # validates arguments (None: JSON schema only)


REGISTRY: dict[str, ToolDef] = {}


def tool(
    *,
    group: str,
    permission: Permission = "auto",
    read_only: bool = False,
    specifier_arg: str | None = None,
) -> Callable[[F], F]:
    """Register an async function `fn(ctx: Ctx, **args)` as a tool. First param must be ctx."""

    def register(fn: F) -> F:
        tool_def = make_tool_def(
            fn,
            group=group,
            permission=permission,
            read_only=read_only,
            specifier_arg=specifier_arg,
        )
        REGISTRY[tool_def.name] = tool_def
        return fn

    return register


def make_tool_def(
    fn: ToolFn,
    *,
    group: str,
    permission: Permission = "auto",
    read_only: bool = False,
    specifier_arg: str | None = None,
) -> ToolDef:
    """Build a ToolDef (schema included) from a function without registering it."""
    model = args_model_for(fn)
    spec = ToolSpec(
        name=fn.__name__, description=first_line(fn.__doc__), parameters=schema_of(model)
    )
    return ToolDef(fn.__name__, group, fn, spec, permission, read_only, specifier_arg, model)


def args_model_for(fn: ToolFn) -> type[BaseModel]:
    """A strict Pydantic model of the function's parameters after `ctx`."""
    params = list(inspect.signature(fn).parameters.values())
    if not params or params[0].name != "ctx":
        raise TypeError(f"tool {fn.__name__}: the first parameter must be ctx")
    hints = typing.get_type_hints(fn, include_extras=True)
    fields: dict[str, Any] = {}
    for param in params[1:]:
        base, description = split_annotated(hints[param.name])
        default = ... if param.default is inspect.Parameter.empty else param.default
        fields[param.name] = (base, Field(default, description=description))
    config = ConfigDict(extra="forbid")
    model: type[BaseModel] = create_model(f"{fn.__name__}_args", __config__=config, **fields)
    return model


def split_annotated(annotation: Any) -> tuple[Any, str | None]:
    """`Annotated[T, "description"]` -> (T, "description"); anything else -> (it, None)."""
    if get_origin(annotation) is Annotated:
        base, *extras = get_args(annotation)
        return base, next((e for e in extras if isinstance(e, str)), None)
    return annotation, None


def first_line(doc: str | None) -> str:
    """The first line of a docstring: the description the model sees."""
    return (doc or "").strip().split("\n", 1)[0].strip()


def schema_of(model: type[BaseModel]) -> dict[str, Any]:
    """JSON Schema of the arguments, with `$ref`s inlined and titles removed."""
    raw = model.model_json_schema()
    defs = raw.pop("$defs", {})
    cleaned: dict[str, Any] = _clean_schema(raw, defs)
    return cleaned


def _clean_schema(node: Any, defs: dict[str, Any]) -> Any:
    # Some providers reject `$ref`, and titles only cost tokens.
    if isinstance(node, list):
        return [_clean_schema(item, defs) for item in node]
    if not isinstance(node, dict):
        return node
    if "$ref" in node:
        target = defs[node["$ref"].rsplit("/", 1)[-1]]
        return _clean_schema({**target, **{k: v for k, v in node.items() if k != "$ref"}}, defs)
    out: dict[str, Any] = {}
    for key, value in node.items():
        if key == "title":
            continue
        if key == "properties":
            out[key] = {name: _clean_schema(sub, defs) for name, sub in value.items()}
        elif key in _DATA_KEYS:
            out[key] = value
        else:
            out[key] = _clean_schema(value, defs)
    return out


def for_role(role: str, cfg: ForgeConfig) -> list[ToolDef]:
    """The tools an agent with this role may use."""
    if role in READ_ONLY_ROLES:
        return [t for t in REGISTRY.values() if t.read_only]
    return list(REGISTRY.values())


async def call_tool(ctx: Ctx, call: ToolCall) -> ToolResult:
    """validate -> permission -> pre_tool hooks -> run -> cap output -> post_tool hooks -> audit"""
    tool_def = REGISTRY.get(call.name)
    if tool_def is None:
        known = ", ".join(sorted(REGISTRY)) or "none"
        result = failure("invalid_args", f"unknown tool '{call.name}'", hint=f"tools: {known}")
        decision = "unknown"
    else:
        result, decision = await _checked_run(ctx, tool_def, call)
    result = await cap_output(ctx.root, result.model_copy(update={"call_id": call.id}))
    if tool_def is not None and decision in ("run", "approved"):
        result = await _post_tool_hooks(ctx, call, result)
    await asyncio.to_thread(_append_audit, ctx, call, result, decision)
    return result


async def _checked_run(ctx: Ctx, tool_def: ToolDef, call: ToolCall) -> tuple[ToolResult, str]:
    decision = "invalid"
    try:
        args = validate_args(tool_def, call.arguments)
        decision = "denied"
        decision = await _authorize(ctx, tool_def, call, args)
        outcome = await ctx.hooks.run("pre_tool", _hook_payload(ctx, call), ctx)
        if outcome.block:
            decision = "blocked"
            raise ToolError("permission_denied", "blocked by a pre_tool hook", body=outcome.message)
    except ToolError as err:
        return err.to_result(call.id), decision
    return await _run_body(ctx, tool_def, call, args), decision


def validate_args(tool_def: ToolDef, arguments: dict[str, Any]) -> dict[str, Any]:
    """Parse arguments with the tool's model; raises ToolError naming each bad field."""
    if "_raw_arguments" in arguments:
        raw = str(arguments["_raw_arguments"])[:500]
        raise ToolError(
            "invalid_args",
            "the arguments are not valid JSON",
            hint="send the arguments as one JSON object",
            body=raw,
        )
    if tool_def.args_model is None:
        return dict(arguments)
    try:
        parsed = tool_def.args_model.model_validate(arguments)
    except ValidationError as exc:
        problems = "; ".join(
            f"{'.'.join(str(p) for p in e['loc']) or '(arguments)'}: {e['msg']}"
            for e in exc.errors()
        )
        raise ToolError("invalid_args", f"invalid arguments: {problems}") from exc
    return {name: getattr(parsed, name) for name in type(parsed).model_fields}


async def _authorize(ctx: Ctx, tool_def: ToolDef, call: ToolCall, args: dict[str, Any]) -> str:
    decision = ctx.permissions.check(tool_def, args, ctx)
    if decision.action == "deny":
        raise ToolError("permission_denied", decision.reason)
    if decision.action == "run":
        return "run"
    approval = await ctx.renderer.approve(call, decision.reason)
    if not approval.allow:
        hint = approval.feedback or "ask the user, or try another approach"
        raise ToolError("permission_denied", "the user declined this call", hint=hint)
    if approval.remember:
        ctx.permissions.remember(tool_def, args)
    return "approved"


async def _run_body(
    ctx: Ctx, tool_def: ToolDef, call: ToolCall, args: dict[str, Any]
) -> ToolResult:
    try:
        value = await tool_def.fn(ctx, **args)
    except ToolError as err:
        return err.to_result(call.id)
    except Exception as exc:
        # A bug in one tool should not end the session; the model sees it and can move on.
        log.exception("tool %s failed", tool_def.name)
        return failure(
            "tool_error", f"internal error in {tool_def.name}: {type(exc).__name__}: {exc}"
        )
    if isinstance(value, str):
        return ToolResult(call_id=call.id, ok=True, text=value)
    return value


async def cap_output(root: Path, result: ToolResult) -> ToolResult:
    """Spill long successful output to `.forge/out/`; cut long failures to head and tail."""
    text = result.text
    if result.ok and len(text) > OUTPUT_CAP_OK:
        name = re.sub(r"[^A-Za-z0-9_.-]", "_", result.call_id or "output")
        rel = f".forge/out/{name}.txt"
        await asyncio.to_thread(_write_text, root / ".forge" / "out" / f"{name}.txt", text)
        note = f"[output truncated: {len(text)} chars total, full output in {rel}]"
        return result.model_copy(
            update={"text": f"{text[:OUTPUT_PREVIEW]}\n{note}", "spill_path": rel}
        )
    if not result.ok and len(text) > OUTPUT_CAP_FAIL:
        half = OUTPUT_CAP_FAIL // 2
        omitted = len(text) - 2 * half
        cut = f"{text[:half]}\n[... {omitted} chars omitted ...]\n{text[-half:]}"
        return result.model_copy(update={"text": cut})
    return result


def _write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


async def _post_tool_hooks(ctx: Ctx, call: ToolCall, result: ToolResult) -> ToolResult:
    payload = {**_hook_payload(ctx, call), "ok": result.ok, "output": result.text[:OUTPUT_PREVIEW]}
    outcome = await ctx.hooks.run("post_tool", payload, ctx)
    if outcome.message:
        return result.model_copy(update={"text": f"{result.text}\nhook: {outcome.message}"})
    return result


def _hook_payload(ctx: Ctx, call: ToolCall) -> dict[str, Any]:
    return {
        "session_id": ctx.session.id,
        "agent_id": ctx.agent_id,
        "cwd": str(ctx.cwd),
        "tool": call.name,
        "args": call.arguments,
    }


def _append_audit(ctx: Ctx, call: ToolCall, result: ToolResult, decision: str) -> None:
    entry = {
        "ts": round(time.time(), 3),
        "session": ctx.session.id,
        "agent": ctx.agent_id,
        "tool": call.name,
        "args": {k: _short(v) for k, v in call.arguments.items()},
        "ok": result.ok,
        "code": result.code,
        "chars": len(result.text),
        "decision": decision,
    }
    path = ctx.root / ".forge" / "audit.log"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as log_file:
        log_file.write(json.dumps(entry, ensure_ascii=False) + "\n")


def _short(value: Any, limit: int = 200) -> Any:
    if isinstance(value, str) and len(value) > limit:
        return value[:limit] + f"... ({len(value)} chars)"
    return value


# =====================================================================================
# SHARED HELPERS FOR TOOL BODIES
# =====================================================================================


def require(condition: bool, message: str) -> None:
    """Raise `invalid_args` with `message` unless `condition` holds."""
    if not condition:
        raise ToolError("invalid_args", message)


def existing_path(ctx: Ctx, path: str) -> Path:
    """Resolve `path`; not_found (with close names as a hint) when it does not exist."""
    require(path.strip() != "", "path must not be empty")
    target = resolve_path(ctx.cwd, path)
    if target.exists():
        return target
    siblings = [p.name for p in target.parent.iterdir()] if target.parent.is_dir() else []
    close = difflib.get_close_matches(target.name, siblings, n=3)
    hint = "did you mean " + ", ".join(close) if close else ""
    raise ToolError("not_found", f"{display_path(ctx.root, target)} does not exist", hint=hint)


def model_has_vision(ctx: Ctx) -> bool:
    """True if the first model of this agent's role can read images."""
    try:
        chain = resolve_role(ctx.role, ctx.cfg)
    except ProviderError:
        return False
    return bool(chain) and chain[0][0].capabilities(chain[0][1]).vision


def diff_counts(old: list[str], new: list[str]) -> tuple[int, int]:
    """Lines added and removed between two versions."""
    added = removed = 0
    matcher = difflib.SequenceMatcher(None, old, new, autojunk=False)
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag in ("replace", "delete"):
            removed += i2 - i1
        if tag in ("replace", "insert"):
            added += j2 - j1
    return added, removed


# =====================================================================================
# FILES
# =====================================================================================

MAX_READ_BYTES = 50 * 1024 * 1024
MAX_WRITE_BYTES = 5 * 1024 * 1024


@tool(group="files", permission="auto", read_only=True, specifier_arg="path")
async def read_file(
    ctx: Ctx,
    path: Annotated[str, "File to read, relative to the current directory or absolute."],
    offset: Annotated[int, "First line to return, 1-based."] = 1,
    limit: Annotated[int, "Maximum number of lines (max 5000)."] = 2000,
    pages: Annotated[str | None, "PDF only: page range like '1-5' (max 20 pages)."] = None,
) -> ToolResult:
    """Read a text file with line numbers; images and PDFs are supported where possible."""
    require(offset >= 1, "offset must be 1 or more")
    require(1 <= limit <= 5000, "limit must be between 1 and 5000")
    target = existing_path(ctx, path)
    display = display_path(ctx.root, target)
    if target.is_dir():
        raise ToolError("invalid_args", f"{display} is a folder", hint="use list_dir")
    if target.stat().st_size > MAX_READ_BYTES:
        raise ToolError("too_large", f"{display} is larger than 50 MB")
    suffix = target.suffix.lower()
    require(pages is None or suffix == ".pdf", "pages is only for PDF files")
    if suffix == ".pdf":
        return ToolResult(call_id="", ok=True, text=await read_pdf(display, target, pages))
    data = await read_bytes(target)
    if suffix in IMAGE_TYPES:
        text, images = read_image(display, target, data, model_has_vision(ctx))
        return ToolResult(call_id="", ok=True, text=text, images=images)
    if suffix == ".ipynb":
        text, full = read_notebook(display, data), True
    elif is_binary(data):
        raise ToolError("binary_file", f"{display} is a binary file")
    else:
        text, full = read_text(display, data, offset, limit)
    ctx.ledger.record(target, data, target.stat().st_mtime_ns, full)
    return ToolResult(call_id="", ok=True, text=text)


@tool(group="files", permission="auto", read_only=False, specifier_arg="path")
async def write_file(
    ctx: Ctx,
    path: Annotated[str, "File to create or overwrite."],
    content: Annotated[str, "The complete new file content."],
) -> str:
    """Create a new file or replace an existing file completely. Prefer edit_file for partial changes."""
    require(path.strip() != "", "path must not be empty")
    target = resolve_path(ctx.cwd, path)
    display = display_path(ctx.root, target)
    check_writable(ctx.root, writable_roots(ctx.root, ctx.cfg), target)
    if len(content.encode("utf-8")) > MAX_WRITE_BYTES:
        raise ToolError("too_large", "content is larger than 5 MB")
    require(not target.is_dir(), f"{display} is a folder")
    old: TextFile | None = None
    if target.exists():
        data = await read_bytes(target)
        entry = ctx.ledger.get(target)
        if entry is None or not entry.full:
            raise ToolError(
                "not_read", f"read all of {display} with read_file before overwriting it"
            )
        if entry.sha256 != sha256_of(data):
            raise ToolError(
                "stale",
                f"{display} changed on disk since your last read",
                hint="read the file again",
            )
        old = decode_text(data)
        if old.text.endswith("\n") and not content.endswith("\n"):
            content += "\n"
    await apply_changes(ctx, [FileChange(target, encode_text(content, old))])
    new_lines = split_lines(content)
    if old is None:
        return f"created {display} ({len(new_lines)} lines)"
    old_lines = split_lines(old.text)
    added, removed = diff_counts(old_lines, new_lines)
    return f"overwrote {display} ({len(old_lines)} -> {len(new_lines)} lines, +{added} -{removed})"


@tool(group="files", permission="auto", read_only=False, specifier_arg="path")
async def edit_file(
    ctx: Ctx,
    path: Annotated[str, "File to edit."],
    old: Annotated[str, "Exact text to replace, including whitespace and indentation."],
    new: Annotated[str, "Replacement text (may be empty to delete)."],
    replace_all: Annotated[bool, "Replace every occurrence instead of exactly one."] = False,
) -> str:
    """Replace an exact, unique piece of text in a file."""
    require(old != "", "old must not be empty")
    require(old != new, "new must differ from old")
    target = resolve_path(ctx.cwd, path)
    display = display_path(ctx.root, target)
    if not target.exists():
        raise ToolError(
            "not_found", f"{display} does not exist", hint="use write_file to create a new file"
        )
    check_writable(ctx.root, writable_roots(ctx.root, ctx.cfg), target)
    entry = ctx.ledger.get(target)
    if entry is None:
        raise ToolError("not_read", f"read {display} with read_file before editing it")
    data = await read_bytes(target)
    if is_binary(data):
        raise ToolError("binary_file", f"{display} is a binary file")
    file = decode_text(data)
    new_text = adapt_newlines(new, file.newline)
    replacement = replace_text(
        file.text, adapt_newlines(old, file.newline), new_text, replace_all, display
    )
    changed_on_disk = entry.sha256 != sha256_of(data)
    await apply_changes(
        ctx, [FileChange(target, encode_text(replacement.text, file, keep_newlines=True))]
    )
    summary = edit_summary(display, replacement, new_text)
    if changed_on_disk:
        summary += (
            "\nnote: file had changed on disk since your last read; re-read before larger edits"
        )
    return summary


@tool(group="files", permission="auto", read_only=True, specifier_arg="path")
async def list_dir(
    ctx: Ctx,
    path: Annotated[str, "Folder to list."] = ".",
    depth: Annotated[int, "How many levels deep (1-5)."] = 2,
    show_hidden: Annotated[bool, "Include dotfiles and ignored files."] = False,
) -> str:
    """Show a folder as a tree, skipping ignored files."""
    require(1 <= depth <= 5, "depth must be between 1 and 5")
    target = existing_path(ctx, path)
    display = display_path(ctx.root, target)
    require(target.is_dir(), f"{display} is not a folder")
    files = await project_files(target, include_ignored=show_hidden, hidden=show_hidden)
    return render_tree(display, build_tree(target, files, depth), depth)


# =====================================================================================
# SEARCH
# =====================================================================================


@tool(group="search", permission="auto", read_only=True, specifier_arg="path")
async def glob(
    ctx: Ctx,
    pattern: Annotated[str, "Glob like '**/*.py' or 'src/**/*.{ts,tsx}'."],
    path: Annotated[str, "Folder to search in."] = ".",
    limit: Annotated[int, "Maximum results (max 1000)."] = 100,
    include_ignored: Annotated[bool, "Also match gitignored files."] = False,
) -> str:
    """Find files by name pattern, newest first."""
    require(pattern.strip() != "" and "\0" not in pattern, "pattern must be a non-empty glob")
    require(1 <= limit <= 1000, "limit must be between 1 and 1000")
    base = existing_path(ctx, path)
    require(base.is_dir(), f"{display_path(ctx.root, base)} is not a folder")
    regexes = [glob_regex(p) for p in expand_braces(pattern)]
    # A pattern that names a dot folder (".github/**") means the user wants hidden files.
    hidden = any(part.startswith(".") and part not in (".", "..") for part in pattern.split("/"))
    files = await project_files(base, include_ignored=include_ignored, hidden=hidden)
    found = [f for f in files if matches_glob(f.relative_to(base).as_posix(), regexes)]
    found.sort(key=lambda f: (-f.stat().st_mtime_ns, str(f)))
    if not found:
        return f'no files match "{pattern}" in {display_path(ctx.root, base)}'
    lines = [display_path(ctx.root, f) for f in found[:limit]]
    if len(found) > limit:
        lines.append(f"[truncated: {len(found)} matches, showing the {limit} newest]")
    return "\n".join(lines)


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
) -> str:
    """Search file contents with a regular expression."""
    require(pattern != "", "pattern must not be empty")
    require(0 <= context <= 10, "context must be between 0 and 10")
    require(1 <= head_limit <= 2000, "head_limit must be between 1 and 2000")
    require(offset >= 0, "offset must be 0 or more")
    base = existing_path(ctx, path)
    query = GrepQuery(pattern, base, glob, type, mode, context, case_insensitive, multiline)
    hits = await search(query, ctx.root)
    return format_hits(hits, query, ctx.root, offset=offset, head_limit=head_limit)


# =====================================================================================
# SHELL
# =====================================================================================

MAX_SHELL_TIMEOUT_S = 600
BENIGN_EXIT1 = frozenset(
    {"grep", "rg", "egrep", "fgrep", "find", "diff", "test", "[", "git-diff", "git-grep"}
)
POWERSHELL_BENIGN_EXIT1 = frozenset({"findstr", "where.exe", "fc.exe", "git-diff", "git-grep"})


@tool(group="shell", permission="ask", read_only=False, specifier_arg="command")
async def bash(
    ctx: Ctx,
    command: Annotated[str, "The bash command to run. Multi-line scripts allowed."],
    timeout_s: Annotated[
        int, "Seconds before the command moves to the background (max 600)."
    ] = 120,
    background: Annotated[bool, "Start as a background job and return immediately."] = False,
    description: Annotated[str, "Up to 80 chars shown to the user, e.g. 'Run unit tests'."] = "",
) -> str:
    """Run a command in the session's persistent bash shell and return exit code, stdout and stderr."""
    return await run_shell(ctx, "bash", command, timeout_s, background, description)


@tool(group="shell", permission="ask", read_only=False, specifier_arg="command")
async def powershell(
    ctx: Ctx,
    command: Annotated[str, "The PowerShell command or script to run."],
    timeout_s: Annotated[
        int, "Seconds before the command moves to the background (max 600)."
    ] = 120,
    background: Annotated[bool, "Start as a background job and return immediately."] = False,
    description: Annotated[str, "Up to 80 chars shown to the user."] = "",
) -> str:
    """Run a command in the session's persistent PowerShell and return exit code, stdout and stderr."""
    return await run_shell(ctx, "powershell", command, timeout_s, background, description)


@tool(group="shell", permission="auto", read_only=True)
async def job_output(
    ctx: Ctx,
    job_id: Annotated[str, "Job id, e.g. 'j3'."],
    since_line: Annotated[int, "First line to return (0-based)."] = 0,
    wait_s: Annotated[float, "Wait up to this long for new output or exit (max 30)."] = 0,
    max_lines: Annotated[int, "Maximum lines to return (max 2000)."] = 400,
) -> str:
    """Read output of a background job and report whether it is still running."""
    require(re.fullmatch(r"j\d+", job_id) is not None, "job_id must look like 'j3'")
    require(since_line >= 0, "since_line must be 0 or more")
    require(0 <= wait_s <= 30, "wait_s must be between 0 and 30")
    require(1 <= max_lines <= 2000, "max_lines must be between 1 and 2000")
    result = await _job_call(ctx.executor.job_output(job_id, since_line))
    deadline = time.monotonic() + wait_s
    while not result.stdout and result.exit_code is None and time.monotonic() < deadline:
        await asyncio.sleep(0.25)
        result = await _job_call(ctx.executor.job_output(job_id, since_line))
    lines = result.stdout.split("\n")[:max_lines] if result.stdout else []
    out = [job_status(job_id, result)]
    if lines:
        last = since_line + len(lines)
        out.append(
            f"lines {since_line + 1}-{last} of {result.total_lines}; next since_line: {last}"
        )
    out += ["--- output ---", *(lines or ["(no new output)"])]
    return "\n".join(out)


@tool(group="shell", permission="auto", read_only=False)
async def job_stop(ctx: Ctx, job_id: Annotated[str, "Job id, e.g. 'j3'."]) -> str:
    """Stop a background job and its child processes."""
    require(re.fullmatch(r"j\d+", job_id) is not None, "job_id must look like 'j3'")
    result = await _job_call(ctx.executor.job_stop(job_id))
    if result.stderr == "already exited":
        return f"job {job_id} had already exited with code {result.exit_code}"
    out = [f"job {job_id} stopped ({result.stderr}, after {result.elapsed_s or 0:.1f}s)"]
    if result.stdout:
        out += [f"--- last {min(20, len(result.stdout.splitlines()))} lines ---", result.stdout]
    return "\n".join(out)


def sandbox_policy(ctx: Ctx) -> SandboxPolicy:
    """The session's sandbox settings as the Executor port expects them."""
    roots = [str(p) for p in writable_roots(ctx.root, ctx.cfg)]
    return SandboxPolicy(
        mode=ctx.cfg.sandbox.mode, writable_roots=roots, network=ctx.cfg.sandbox.network
    )


async def run_shell(
    ctx: Ctx, kind: ShellKind, command: str, timeout_s: int, background: bool, description: str
) -> str:
    """Shared body of the bash and powershell tools."""
    require(
        command.strip() != "" and len(command) <= 100_000,
        "command must be 1-100,000 characters, not only whitespace",
    )
    require(1 <= timeout_s <= MAX_SHELL_TIMEOUT_S, "timeout_s must be between 1 and 600")
    require(
        len(description) <= 80 and "\n" not in description,
        "description must be one line of at most 80 characters",
    )
    if find_shell(kind) is None:
        hint = (
            "install Git for Windows (Git Bash)"
            if kind == "bash"
            else "install PowerShell 7 (pwsh)"
        )
        raise ToolError("unsupported", f"{kind} is not available on this machine", hint=hint)
    started = time.monotonic()
    cmd = Command(script=command, shell=kind, cwd=str(ctx.cwd), timeout_s=timeout_s)
    result = await ctx.executor.run(cmd, sandbox_policy(ctx), background=background)
    if background:
        label = description or command.strip().splitlines()[0][:60]
        return job_started(result, label)
    if result.sandbox_denied:
        raise ToolError(
            "sandbox_denied",
            "the sandbox blocked this command",
            hint="the sandbox blocked network or path access; ask the user or use another approach",
            body=result.stderr,
        )
    if result.timed_out:
        if result.job_id is None:
            raise ToolError(
                "timeout",
                f"the command was stopped after {timeout_s}s",
                hint="sleep commands are not moved to the background",
            )
        return timed_out(result, timeout_s)
    body = shell_body(ctx, result, time.monotonic() - started)
    if shell_succeeded(kind, command, result.exit_code):
        return body
    raise ToolError("exit_nonzero", f"exit code {result.exit_code}", body=body)


def shell_body(ctx: Ctx, result: CommandResult, duration_s: float) -> str:
    """`exit_code`, `duration`, changed `cwd`, then the non-empty output sections."""
    lines = [f"exit_code: {result.exit_code}", f"duration: {duration_s:.1f}s"]
    note = ""
    if result.cwd:
        new_cwd = Path(result.cwd).resolve()
        if not is_within(new_cwd, ctx.root):
            note = f"note: cwd reset to {display_path(ctx.root, ctx.cwd)}"
        elif new_cwd != ctx.cwd:
            ctx.cwd = new_cwd
            lines.append(f"cwd: {display_path(ctx.root, new_cwd)}")
    if result.stdout.strip():
        lines += ["--- stdout ---", result.stdout.rstrip("\n")]
    if result.stderr.strip():
        lines += ["--- stderr ---", result.stderr.rstrip("\n")]
    if note:
        lines.append(note)
    return "\n".join(lines)


def shell_succeeded(kind: ShellKind, command: str, exit_code: int | None) -> bool:
    """Exit 0, or a failure code that is normal for the program (grep finding nothing)."""
    if exit_code == 0:
        return True
    program = first_program(command)
    if kind == "powershell" and program == "robocopy":
        return exit_code is not None and exit_code < 8
    benign = BENIGN_EXIT1 if kind == "bash" else POWERSHELL_BENIGN_EXIT1
    return exit_code == 1 and program in benign


def first_program(command: str) -> str:
    """The program a command line starts with, e.g. `grep` or `git-diff`."""
    try:
        tokens = shlex.split(command)
    except ValueError:
        tokens = command.split()
    while tokens and re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", tokens[0]):
        tokens.pop(0)  # leading VAR=value assignments
    if not tokens:
        return ""
    name = re.split(r"[\\/]", tokens[0])[-1].lower()
    return f"git-{tokens[1]}" if name == "git" and len(tokens) > 1 else name


def job_started(result: CommandResult, label: str) -> str:
    """The reply when a background job starts."""
    job = result.job_id
    return (
        f"started job {job} (pid {result.pid}): {label}\n"
        f"log: .forge/jobs/{job}.log\n"
        f'read output with job_output("{job}"); stop with job_stop("{job}")'
    )


def timed_out(result: CommandResult, timeout_s: int) -> str:
    """The reply when a command outlived its timeout and moved to the background."""
    job = result.job_id
    out = [
        f"timeout: still running after {timeout_s}s, moved to background as job {job}",
        f'read output with job_output("{job}"); stop with job_stop("{job}")',
    ]
    if result.stdout.strip():
        out += ["--- stdout so far ---", result.stdout.rstrip("\n")]
    return "\n".join(out)


def job_status(job_id: str, result: CommandResult) -> str:
    """`job j3: running (pid 41233, 52.3s)` or `job j3: exited with code 0 after 61.0s`."""
    elapsed = result.elapsed_s or 0.0
    if result.exit_code is None:
        return f"job {job_id}: running (pid {result.pid}, {elapsed:.1f}s)"
    return f"job {job_id}: exited with code {result.exit_code} after {elapsed:.1f}s"


async def _job_call(call: Awaitable[CommandResult]) -> CommandResult:
    try:
        return await call
    except JobNotFoundError as exc:
        known = ", ".join(exc.known) or "none"
        raise ToolError(
            "not_found", f"no background job {exc.args[0]}", hint=f"known jobs: {known}"
        ) from exc


# =====================================================================================
# PLAN AND INTERACTION
# =====================================================================================


class QuestionIn(BaseModel):
    """A question as the model writes it (validated into plan.Question)."""

    text: str = Field(max_length=300)
    kind: Literal["choice", "multi", "text", "confirm"]
    options: list[str] = []  # 2-6 for choice/multi, each <= 80 chars, unique
    default: str | None = (
        None  # choice: one option; multi: options joined by ", "; confirm: "yes"/"no"
    )
    why: str = Field(max_length=120)


@tool(group="plan", permission="auto", read_only=True)
async def ask_user(
    ctx: Ctx,
    questions: Annotated[list[QuestionIn], "1-4 questions, most important first."],
) -> str:
    """Ask the user questions whose answers change the result. Never ask what you can find in the repo."""
    if ctx.agent_id != "main":
        raise ToolError(
            "unsupported",
            "only the lead agent may ask the user",
            hint="report the question to the lead with send_message",
        )
    require(1 <= len(questions) <= 4, "ask 1 to 4 questions")
    for number, question in enumerate(questions, start=1):
        check_question(number, question)
    asked = [Question(**q.model_dump()) for q in questions]
    answers = await ask(ctx, asked)
    if answers is None:
        raise ToolError(
            "cancelled",
            "user dismissed the questions",
            hint="continue with your best judgment and state your assumptions",
        )
    lines = []
    suffix = " (default, headless)" if ctx.headless else ""
    for number, (asked_q, answer) in enumerate(zip(asked, answers, strict=True), start=1):
        lines += [f"{number}. {asked_q.text}", f"   answer: {answer or '(empty)'}{suffix}"]
    return "\n".join(lines)


def check_question(number: int, question: QuestionIn) -> None:
    """Options must fit the kind, and the default must be a valid answer."""
    where = f"question {number}"
    options = question.options
    if question.kind in ("choice", "multi"):
        require(2 <= len(options) <= 6, f"{where}: {question.kind} needs 2 to 6 options")
        require(len(set(options)) == len(options), f"{where}: options must be unique")
        require(
            all(len(o) <= 80 for o in options), f"{where}: options are limited to 80 characters"
        )
    else:
        require(not options, f"{where}: only choice and multi questions have options")
    default = question.default
    if default is None:
        return
    if question.kind == "choice":
        require(default in options, f"{where}: the default must be one of the options")
    elif question.kind == "multi":
        require(
            all(p.strip() in options for p in default.split(",")),
            f"{where}: every default must be an option",
        )
    elif question.kind == "confirm":
        require(default in ("yes", "no"), f"{where}: a confirm default is 'yes' or 'no'")


class StepIn(BaseModel):
    """A plan step as the planner writes it."""

    title: str = Field(max_length=100)
    detail: str
    files: list[str] = []
    depends_on: list[int] = []  # 1-based numbers of earlier steps in this list
    check: str  # shell command, or "review: <criterion>"
    role: str = "coder"


PLANNING_ROLES = frozenset({"planner", "replanner"})
BUILTIN_ROLES = frozenset({"coder", "tester", "reviewer", "researcher", "explore", "lead"})


@tool(group="plan", permission="auto", read_only=True)
async def submit_plan(
    ctx: Ctx,
    steps: Annotated[list[StepIn], "Ordered steps, 1-30."],
    explanation: Annotated[str, "One or two sentences on the approach."] = "",
) -> str:
    """Submit the implementation plan for user approval."""
    if ctx.role not in PLANNING_ROLES:
        raise ToolError("unsupported", "only the planner can submit a plan")
    spec = ctx.session.spec
    if spec is None:
        raise ToolError("invalid_args", "there is no task specification to plan for yet")
    require(1 <= len(steps) <= 30, "submit 1 to 30 steps")
    plan = build_plan(ctx, spec, steps)
    problems = plan.validate_graph() + plan_problems(ctx, plan)
    if problems:
        raise ToolError(
            "invalid_args", "the plan is not valid", body="\n".join(f"- {p}" for p in problems)
        )
    if not ctx.headless:
        call = ToolCall(id="plan", name="submit_plan", arguments={"steps": len(plan.steps)})
        approval = await ctx.renderer.approve(
            call, f"approve this plan?\n{checklist(plan)}\n{explanation}".rstrip()
        )
        if not approval.allow:
            ctx.state.plan_rejected = not approval.feedback
            hint = approval.feedback or "the user rejected the plan; stop and say so"
            raise ToolError("permission_denied", "the user did not approve the plan", hint=hint)
    ctx.session.plan = plan
    await ctx.store.save_session(ctx.session)
    await ctx.bus.publish(
        PlanUpdated(session_id=ctx.session.id, agent_id=ctx.agent_id, ts=time.time(), plan=plan)
    )
    return plan_summary(plan)


def build_plan(ctx: Ctx, spec: TaskSpec, steps: list[StepIn]) -> Plan:
    """New Steps with ids; a replan keeps the finished steps and numbers on after them."""
    old = ctx.session.plan
    kept = (
        [s for s in old.steps if s.status in ("done", "skipped")]
        if old and ctx.role == "replanner"
        else []
    )
    first = max((int(s.id[1:]) for s in kept if s.id[1:].isdigit()), default=0) + 1
    ids = [f"s{first + i}" for i in range(len(steps))]
    new_steps = [
        Step(
            id=ids[i],
            title=s.title,
            detail=s.detail,
            files=s.files,
            depends_on=[ids[n - 1] if 1 <= n <= len(ids) else f"#{n}" for n in s.depends_on],
            check=s.check,
            role=s.role,
        )
        for i, s in enumerate(steps)
    ]
    version = old.version + 1 if old else 1
    return Plan(spec=spec, steps=[*kept, *new_steps], version=version)


def plan_problems(ctx: Ctx, plan: Plan) -> list[str]:
    """Unknown roles and files outside the project."""
    roles = BUILTIN_ROLES | set(ctx.cfg.roles)
    problems = [f"{s.id} has unknown role '{s.role}'" for s in plan.steps if s.role not in roles]
    for step in plan.steps:
        for name in step.files:
            if not is_within(resolve_path(ctx.root, name), ctx.root):
                problems.append(f"{step.id} lists {name}, which is outside the project")
    return problems


def plan_summary(plan: Plan) -> str:
    """`plan approved: N steps (version V)` and one aligned line per step."""
    rows = []
    for step in plan.steps:
        after = f" (after {', '.join(step.depends_on)})" if step.depends_on else ""
        rows.append((f"{step.id} [{step.role}]", f"{step.title}{after}", step.check))
    left = max(len(r[0]) for r in rows)
    middle = max(len(r[1]) for r in rows)
    lines = [f"plan approved: {len(plan.steps)} steps (version {plan.version})"]
    lines += [f"{a.ljust(left)}  {b.ljust(middle)}  -> check: {c}" for a, b, c in rows]
    return "\n".join(lines)
