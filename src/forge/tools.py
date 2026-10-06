"""tools.py — every tool Forge can use, in one file.

Table of contents:
  FRAMEWORK  ToolDef, REGISTRY, tool(), make_tool_def(), for_role(), call_tool()
  FILES      read_file, write_file, edit_file, apply_patch, list_dir
  SEARCH     glob, grep, repo_map
  SHELL      bash, powershell, job_output, job_stop
  WEB        web_fetch, web_search
  PLAN       ask_user, submit_plan, update_plan, finish_step
  AGENTS     spawn_agent, send_message, list_agents, stop_agent
  BOARD      read_board, claim_task, update_task
  MEMORY     remember, recall
  MCP        list_mcp_resources, read_mcp_resource, tool_search (+ mcp__<server>__<tool>)

Each tool is a plain async function `fn(ctx, **args)` with a decorator. The decorator
reads the signature and docstring and generates the JSON schema, so a tool is written
once and never described twice.
"""

import asyncio
import contextvars
import datetime
import difflib
import inspect
import json
import logging
import os
import re
import shlex
import time
import typing
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Any, Literal, TypeVar, get_args, get_origin

from pydantic import BaseModel, ConfigDict, Field, ValidationError, create_model, field_validator

from forge import prompts
from forge.checks import sandbox_policy, save_plan, settle_step
from forge.config import ForgeConfig, forge_home
from forge.ctx import Ctx, McpTools, Team
from forge.events import PlanUpdated, TodosUpdated, ToolOutput
from forge.modelcall import complete
from forge.plan import Plan, Question, Step, TaskSpec, checklist
from forge.ports import (
    Browser,
    BrowserError,
    Command,
    CommandResult,
    JobNotFoundError,
    PageView,
    SandboxPolicy,
)
from forge.providers.base import ProviderError, ToolCall, ToolResult, ToolSpec, text_message
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
from forge.runtime.patch import FileOp, PatchSyntaxError, apply_hunks, parse_patch
from forge.runtime.readers import IMAGE_TYPES, read_image, read_notebook, read_pdf, read_text
from forge.runtime.repomap import build_map, render_map
from forge.runtime.search import GrepQuery, format_hits, search
from forge.runtime.secrets import mask_secrets
from forge.runtime.shell import ShellKind, find_shell
from forge.runtime.tree import build_tree, render_tree
from forge.runtime.web import (
    SearchResult,
    checked_url,
    fetch_page,
    filter_results,
    search_http,
    size_text,
)
from forge.todos import Todo, todo_lines, todo_problems

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
LEAD_ONLY_TOOLS = frozenset({"ask_user", "spawn_agent", "submit_plan", "research"})
BOARD_TOOLS = frozenset({"read_board", "claim_task", "update_task"})
AGENT_TOOLS = frozenset({"spawn_agent", "send_message", "list_agents", "stop_agent"})
TOOL_GROUPS = frozenset(
    {"files", "search", "shell", "web", "browser", "plan", "agents", "memory", "mcp"}
)
BROWSER_ROLE_TOOLS = frozenset({"web_search"})  # besides the browser group
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
# The id of the call whose body is running, so a body can label its live output (per task).
CALL_ID: contextvars.ContextVar[str] = contextvars.ContextVar("forge_call_id", default="")


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
    if role == "browser":
        return [
            t for t in REGISTRY.values() if t.group == "browser" or t.name in BROWSER_ROLE_TOOLS
        ]
    tools = [t for t in REGISTRY.values() if t.group != "browser"]
    if role in READ_ONLY_ROLES:
        return [t for t in tools if t.read_only]
    return tools


def hidden_mcp_tool(ctx: Ctx, tool_def: ToolDef) -> bool:
    """A deferred MCP tool may still be called by name (the model may know it from the list)."""
    return (
        tool_def.group == "mcp"
        and tool_def.name.startswith("mcp__")
        and (tool_def.read_only or ctx.role not in READ_ONLY_ROLES)
    )


def agent_tools(ctx: Ctx, role: str) -> list[ToolDef]:
    """The role's tools (an agent file may list them); sub-agents never get lead-only tools."""
    custom = ctx.state.custom_roles.get(role)
    if custom is not None and custom.tools is not None:
        tools = [t for t in REGISTRY.values() if t.name in custom.tools or t.group in custom.tools]
    else:
        tools = for_role(role, ctx.cfg)
    if ctx.agent_id != "main":
        tools = [t for t in tools if t.name not in LEAD_ONLY_TOOLS]
    if not ctx.state.team_mode:
        tools = [t for t in tools if t.name not in BOARD_TOOLS]
    if ctx.state.mode == "solo":
        tools = [t for t in tools if t.name not in AGENT_TOOLS]
    return [*tools, *mcp_tools(ctx, role)] if ctx.state.mcp else drop_mcp(tools)


def drop_mcp(tools: list[ToolDef]) -> list[ToolDef]:
    """Without MCP servers the MCP tools are pointless."""
    return [t for t in tools if t.group != "mcp"]


def mcp_tools(ctx: Ctx, role: str) -> list[ToolDef]:
    """The MCP servers' loaded tools this role may use (read-only roles: read-only tools)."""
    hub = ctx.state.mcp
    if hub is None:
        return []
    tools = hub.visible_tools()
    custom = ctx.state.custom_roles.get(role)
    if custom is not None and custom.tools is not None:
        return [t for t in tools if t.name in custom.tools or "mcp" in custom.tools]
    return [t for t in tools if t.read_only or role not in READ_ONLY_ROLES]


SEQUENTIAL_GROUPS = frozenset({"browser"})  # read-only, but each step depends on the last
NOT_EARLY = frozenset({"research"})  # starts an agent; wait until the model has finished


def can_run_concurrently(tool_def: ToolDef | None) -> bool:
    """Read-only tools that do not depend on each other may run at the same time."""
    return tool_def is not None and tool_def.read_only and tool_def.group not in SEQUENTIAL_GROUPS


def can_start_early(ctx: Ctx, call: ToolCall) -> bool:
    """True if the call may start while the model is still writing: safe and no approval."""
    tool_def = REGISTRY.get(call.name)
    if (
        not can_run_concurrently(tool_def)
        or tool_def is None
        or tool_def.permission != "auto"
        or tool_def.name in NOT_EARLY
        or tool_def not in agent_tools(ctx, ctx.role)
    ):
        return False
    try:
        args = validate_args(tool_def, call.arguments)
    except ToolError:
        return False
    return ctx.permissions.check(tool_def, args, ctx).action == "run"


def find_tool(ctx: Ctx, name: str) -> ToolDef | None:
    """A built-in tool, or one of the session's MCP tools."""
    found = REGISTRY.get(name)
    if found is None and ctx.state.mcp is not None:
        found = ctx.state.mcp.tool(name)
    return found


async def call_tool(ctx: Ctx, call: ToolCall) -> ToolResult:
    """validate -> permission -> pre_tool hooks -> run -> cap output -> post_tool hooks -> audit"""
    tool_def = find_tool(ctx, call.name)
    if tool_def is None:
        known = ", ".join(sorted(REGISTRY)) or "none"
        result = failure("invalid_args", f"unknown tool '{call.name}'", hint=f"tools: {known}")
        decision = "unknown"
    elif tool_def not in agent_tools(ctx, ctx.role) and not hidden_mcp_tool(ctx, tool_def):
        result = failure("unsupported", f"{call.name} is not available to the {ctx.role} role")
        decision = "refused"
    else:
        result, decision = await _checked_run(ctx, tool_def, call)
    masked = mask_secrets(result.text)  # before capping, so spill files are masked too
    result = await cap_output(
        ctx.root, result.model_copy(update={"call_id": call.id, "text": masked})
    )
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
    token = CALL_ID.set(call.id)
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
    finally:
        CALL_ID.reset(token)
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
    check_writable(
        ctx.root, writable_roots(ctx.root, ctx.cfg), target
    )  # before revealing existence
    if not target.exists():
        raise ToolError(
            "not_found", f"{display} does not exist", hint="use write_file to create a new file"
        )
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


MAX_PATCH_BYTES = 2 * 1024 * 1024


@tool(group="files", permission="auto", read_only=False)
async def apply_patch(
    ctx: Ctx,
    patch: Annotated[
        str, "Patch in the Forge/Codex format, from *** Begin Patch to *** End Patch."
    ],
) -> str:
    """Add, update, move and delete several files in one atomic change."""
    if len(patch.encode("utf-8")) > MAX_PATCH_BYTES:
        raise ToolError("too_large", "the patch is larger than 2 MB")
    try:
        ops = parse_patch(patch)
    except PatchSyntaxError as err:
        raise ToolError("invalid_args", "the patch could not be parsed", hint=str(err)) from err
    changes: list[FileChange] = []
    summary: list[str] = []
    for op in ops:
        file_changes, line = await patch_file(ctx, op)
        changes += file_changes
        summary.append(line)
    paths = [c.path for c in changes]
    require(len(paths) == len(set(paths)), "the patch changes the same file more than once")
    await apply_changes(ctx, changes)  # only reached when every file succeeded
    return f"applied patch: {len(ops)} files\n" + "\n".join(summary)


async def patch_file(ctx: Ctx, op: FileOp) -> tuple[list[FileChange], str]:
    """The changes for one file operation, and its summary line."""
    target = resolve_path(ctx.cwd, op.path)
    display = display_path(ctx.root, target)
    check_writable(ctx.root, writable_roots(ctx.root, ctx.cfg), target)
    await authorize_path(ctx, "apply_patch", target)
    if op.kind == "add":
        if target.exists():
            raise ToolError("invalid_args", f"{display} already exists", hint="use *** Update File")
        content = "".join(line + "\n" for line in op.added)
        return [FileChange(target, encode_text(content, None))], f"  A {display} (+{len(op.added)})"
    file = await read_for_patch(ctx, target, display)
    if op.kind == "delete":
        return [FileChange(target, None)], f"  D {display}"
    old_lines = split_lines(file.text)
    new_lines = apply_hunks(old_lines, op.hunks, display)
    ends_with_break = file.text.endswith("\n") or not file.text
    content = "\n".join(new_lines) + ("\n" if new_lines and ends_with_break else "")
    added, removed = diff_counts(old_lines, new_lines)
    data = encode_text(content, file)
    if op.move_to is None:
        return [FileChange(target, data)], f"  M {display} (+{added} -{removed})"
    dest = resolve_path(ctx.cwd, op.move_to)
    check_writable(ctx.root, writable_roots(ctx.root, ctx.cfg), dest)
    await authorize_path(ctx, "apply_patch", dest)
    moved = display_path(ctx.root, dest)
    if dest.exists():
        raise ToolError("invalid_args", f"cannot move to {moved}: it already exists")
    changes = [FileChange(dest, data), FileChange(target, None)]
    return changes, f"  R {display} -> {moved} (+{added} -{removed})"


async def authorize_path(ctx: Ctx, tool_name: str, path: Path) -> None:
    """Permission check for one path of a multi-file tool; raises when refused."""
    decision = ctx.permissions.check_path(REGISTRY[tool_name], str(path), ctx.root)
    if decision.action == "run":
        return
    display = display_path(ctx.root, path)
    if decision.action == "deny":
        raise ToolError("permission_denied", f"{display}: {decision.reason}")
    call = ToolCall(id="", name=tool_name, arguments={"path": display})
    if not (await ctx.renderer.approve(call, decision.reason)).allow:
        raise ToolError("permission_denied", f"the user declined changing {display}")


async def read_for_patch(ctx: Ctx, target: Path, display: str) -> TextFile:
    """The decoded file an Update or Delete works on; it must exist and have been read."""
    if not target.is_file():
        raise ToolError("not_found", f"{display} does not exist")
    if ctx.ledger.get(target) is None:
        raise ToolError("not_read", f"read {display} with read_file before patching it")
    data = await read_bytes(target)
    if is_binary(data):
        raise ToolError("binary_file", f"{display} is a binary file")
    return decode_text(data)


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


@tool(group="search", permission="auto", read_only=True, specifier_arg="path")
async def repo_map(
    ctx: Ctx,
    path: Annotated[str, "Folder to map."] = ".",
    max_tokens: Annotated[int, "Size budget for the map (200-20000)."] = 2000,
    include_private: Annotated[bool, "Include names starting with '_'."] = False,
) -> str:
    """Outline of the most important files with their classes, functions and signatures."""
    require(200 <= max_tokens <= 20000, "max_tokens must be between 200 and 20000")
    base = existing_path(ctx, path)
    require(base.is_dir(), f"{display_path(ctx.root, base)} is not a folder")
    files = await project_files(base)
    repo = await asyncio.to_thread(build_map, ctx.root, files)
    return render_map(repo, max_tokens, include_private)


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


@tool(group="shell", permission="ask", read_only=False, specifier_arg="command")
async def monitor(
    ctx: Ctx,
    command: Annotated[
        str, "Command whose output to watch, e.g. a log tail or a watch-mode test run."
    ],
    description: Annotated[str, "Up to 80 chars naming what is watched, e.g. 'dev server errors'."],
    filter: Annotated[
        str, "Regular expression: only matching lines are sent (empty: every line)."
    ] = "",
    timeout_s: Annotated[int, "Seconds until the command is stopped (max 3600)."] = 600,
) -> str:
    """Run a command in the background and receive each new output line as a message."""
    require(command.strip() != "", "command must not be empty")
    require(
        0 < len(description) <= 80 and "\n" not in description, "description: one line, 1-80 chars"
    )
    require(1 <= timeout_s <= 3600, "timeout_s must be between 1 and 3600")
    try:
        re.compile(filter)
    except re.error as exc:
        raise ToolError("invalid_args", f"filter is not a valid regular expression: {exc}") from exc
    kind: ShellKind = "bash" if find_shell("bash") else "powershell"
    cmd = Command(script=command, shell=kind, cwd=str(ctx.cwd), timeout_s=timeout_s)
    result = await ctx.executor.run(cmd, sandbox_policy(ctx), background=True)
    if result.job_id is None:
        raise ToolError("exit_nonzero", "the command could not start", body=result.stderr)
    if ctx.state.team is not None:
        ctx.state.team.note_job(ctx.agent_id, result.job_id)
    ctx.state.job_labels[result.job_id] = description
    watched = ctx.state.monitors.start(ctx, result.job_id, description, filter, timeout_s)
    return (
        f"monitor {watched.id} started (job {result.job_id}): {description}\n"
        "new output lines arrive as messages; when you have nothing else to do, end your turn "
        f'and you will be woken by them; stop with monitor_stop("{watched.id}")'
    )


@tool(group="shell", permission="auto", read_only=False)
async def monitor_stop(ctx: Ctx, monitor_id: Annotated[str, "Monitor id, e.g. 'm1'."]) -> str:
    """Stop a monitor and its command."""
    stopped = await ctx.state.monitors.stop(ctx, monitor_id)
    return f"stopped monitor {stopped.id} ({stopped.description}) after {stopped.sent} lines"


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
    live = None if background else LiveOutput(ctx, CALL_ID.get())
    try:
        result = await ctx.executor.run(
            cmd, sandbox_policy(ctx), background=background, on_output=live.write if live else None
        )
    finally:
        if live is not None:
            await live.close()
    if background:
        if ctx.state.team is not None and result.job_id:
            ctx.state.team.note_job(ctx.agent_id, result.job_id)
        label = description or command.strip().splitlines()[0][:60]
        if result.job_id:
            ctx.state.job_labels[result.job_id] = label
        return job_started(result, label)
    if result.sandbox_denied:
        result = await outside_sandbox(ctx, cmd, result)
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


class LiveOutput:
    """Publishes a running command's output as ToolOutput events: whole lines, masked, throttled."""

    MAX_LINES = 500  # per call; the final result still has everything

    def __init__(self, ctx: Ctx, call_id: str, interval_s: float = 0.2) -> None:
        self.ctx = ctx
        self.call_id = call_id
        self.interval_s = interval_s
        self.pending = ""
        self.lines = 0
        self.flusher: asyncio.Task[None] | None = None

    def write(self, text: str) -> None:
        """Take new output; a flush is scheduled at most every `interval_s`."""
        self.pending += text
        if self.flusher is None and "\n" in self.pending:
            self.flusher = asyncio.get_running_loop().create_task(self._flush_later())

    async def close(self) -> None:
        """Publish what is left (also an unfinished last line)."""
        if self.flusher is not None:
            self.flusher.cancel()
            self.flusher = None
        if self.pending.strip():  # a lone newline is the shell wrapper's, not the command's
            await self._publish(self.pending)
        self.pending = ""

    async def _flush_later(self) -> None:
        await asyncio.sleep(self.interval_s)
        cut = self.pending.rfind("\n") + 1
        text, self.pending = self.pending[:cut], self.pending[cut:]
        self.flusher = None
        await self._publish(text)

    async def _publish(self, text: str) -> None:
        if not text or self.lines >= self.MAX_LINES:
            return
        lines = text.splitlines(keepends=True)[: self.MAX_LINES - self.lines]
        self.lines += len(lines)
        if self.lines >= self.MAX_LINES:
            lines.append("[live output stops here; the result has all of it]\n")
        await self.ctx.bus.publish(
            ToolOutput(
                session_id=self.ctx.session.id,
                agent_id=self.ctx.agent_id,
                ts=time.time(),
                call_id=self.call_id,
                text=mask_secrets("".join(lines)),
            )
        )


async def outside_sandbox(ctx: Ctx, cmd: Command, blocked: CommandResult) -> CommandResult:
    """Offer to rerun a command the sandbox blocked without it; sandbox_denied if not allowed."""
    output = "\n".join(part for part in (blocked.stdout, blocked.stderr) if part.strip())
    refused = ToolError(
        "sandbox_denied",
        "the sandbox blocked this command (network or a path outside the writable folders)",
        hint="use another approach, or ask the user to allow it",
        body=output,
    )
    if ctx.cfg.approval.policy == "never" or ctx.cfg.sandbox.mode == "full-access":
        raise refused
    call = ToolCall(id="", name=cmd.shell, arguments={"command": cmd.script or ""})
    approval = await ctx.renderer.approve(
        call, "the sandbox blocked it; run it without the sandbox?"
    )
    if not approval.allow:
        raise refused
    unrestricted = SandboxPolicy(mode="full-access", writable_roots=[], network=True)
    return await ctx.executor.run(cmd, unrestricted)


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
# WEB
# =====================================================================================


@tool(group="web", permission="ask", read_only=True, specifier_arg="url")
async def web_fetch(
    ctx: Ctx,
    url: Annotated[str, "Full http(s) URL."],
    question: Annotated[
        str | None, "If set, return only the answer to this question, extracted from the page."
    ] = None,
    max_chars: Annotated[
        int, "Maximum characters of page content to return (1000-100000)."
    ] = 20000,
) -> str:
    """Download a web page and return it as Markdown, or answer a question about it."""
    require(1000 <= max_chars <= 100_000, "max_chars must be between 1000 and 100000")
    page = await fetch_page(ctx.root, url)
    if page.redirect:
        return f"redirected to {page.redirect}; call web_fetch again with that URL if you trust it"
    content = page.content
    if len(content) > max_chars:
        content = content[:max_chars] + f"\n[content cut at {max_chars} chars]"
    cached = " (cached)" if page.cached else ""
    head = (
        f"url: {page.url} ({page.status}, {page.content_type}, {size_text(page.size)} -> "
        f"{len(page.content):,} chars){cached}"
    )
    lines = [head] + ([f"title: {page.title}"] if page.title else [])
    if question is None:
        return "\n".join([*lines, "--- content ---", content])
    system = prompts.render("web_extract", question=question, page=content)
    answer, _ = await complete(ctx, "compressor", system, [text_message("user", question)])
    return "\n".join([*lines, "--- answer ---", answer.text().strip()])


@tool(group="web", permission="ask", read_only=True)
async def web_search(
    ctx: Ctx,
    query: Annotated[str, "Search query."],
    allowed_domains: Annotated[list[str] | None, "Only return results from these domains."] = None,
    blocked_domains: Annotated[list[str] | None, "Never return results from these domains."] = None,
    max_results: Annotated[int, "Number of results (1-20)."] = 8,
) -> str:
    """Search the web and return titles, URLs and snippets. Use web_fetch to read a result."""
    require(1 <= len(query.strip()) <= 400, "query must be 1-400 characters")
    require(1 <= max_results <= 20, "max_results must be between 1 and 20")
    require(
        not (allowed_domains and blocked_domains),
        "use allowed_domains or blocked_domains, not both",
    )
    allowed, blocked = allowed_domains or [], blocked_domains or []
    if ctx.state.web_searches >= ctx.cfg.limits.max_web_searches:
        raise ToolError(
            "limit_reached",
            f"{ctx.cfg.limits.max_web_searches} web searches in this session",
            hint="continue with the information you have",
        )
    ctx.state.web_searches += 1
    backend, results = await run_search(ctx, query, max_results, allowed, blocked)
    results = filter_results(results, allowed, blocked, max_results)
    if not results:
        return f'no results for "{query}"'
    lines = [f'results for "{query}" (backend: {backend}, {len(results)} results)']
    for number, hit in enumerate(results, start=1):
        lines += [f"{number}. {hit.title}", f"   {hit.url}"]
        if hit.snippet:
            lines.append(f"   {' '.join(hit.snippet.split())[:300]}")
    return "\n".join(lines)


async def run_search(
    ctx: Ctx, query: str, max_results: int, allowed: list[str], blocked: list[str]
) -> tuple[str, list[SearchResult]]:
    """(backend name, raw results) from the configured backend."""
    web_cfg = ctx.cfg.web
    backend: str | None = web_cfg.search_backend
    if backend == "native":
        native = await native_search(ctx, query, max_results, allowed, blocked)
        if native is not None:
            return "native", native
        backend = web_cfg.fallback_backend  # the model has no search tool of its own
    key = os.environ.get(web_cfg.search_api_key_env, "") if web_cfg.search_api_key_env else ""
    if backend is not None and key:
        return backend, await search_http(backend, key, query, max_results, allowed, blocked)
    raise ToolError(
        "unsupported",
        "no web search backend is available",
        hint="set [web] search_backend in forge.toml",
    )


async def native_search(
    ctx: Ctx, query: str, max_results: int, allowed: list[str], blocked: list[str]
) -> list[SearchResult] | None:
    """Results from the agent's own model's search tool, or None when it has none."""
    try:
        chain = resolve_role(ctx.role, ctx.cfg)
    except ProviderError:
        return None
    if not chain:
        return None
    provider, model = chain[0]
    search_web = getattr(provider, "search_web", None)
    if search_web is None or not provider.capabilities(model).web_search:
        return None
    try:
        hits = await search_web(model, query, max_results, allowed, blocked)
    except ProviderError as err:
        raise ToolError("network", f"native search failed ({err.kind}): {err}") from err
    return [SearchResult(title, url, snippet) for title, url, snippet in hits]


RESEARCH_TURNS = {"normal": 15, "deep": 40}


@tool(group="web", permission="auto", read_only=True)
async def research(
    ctx: Ctx,
    question: Annotated[
        str,
        "The question with everything the researcher needs: what you want to know, why, and constraints such as versions or platforms.",
    ],
    browser: Annotated[
        bool,
        "Use a real browser with screenshots; only for pages that need JavaScript, interaction or a visual check.",
    ] = False,
    depth: Annotated[
        Literal["normal", "deep"],
        "normal: one focused question; deep: a broad comparison or many sources.",
    ] = "normal",
) -> str:
    """Hand a research question to a researcher sub-agent; returns its report with sources."""
    if browser:
        check_browser_agent(ctx)
    return await team_of(ctx).spawn(
        ctx,
        "browser" if browser else "researcher",
        question,
        background=False,
        isolation="none",
        max_turns=RESEARCH_TURNS[depth],
        name=None,
    )


def check_browser_agent(ctx: Ctx) -> None:
    """A browser agent needs a browser and a model that can see screenshots."""
    if ctx.state.browser_factory is None:
        raise ToolError(
            "unsupported", "no browser is available", hint="call research without browser=true"
        )
    try:
        chain = resolve_role("browser", ctx.cfg)
    except ProviderError as err:
        raise ToolError("unsupported", f"the browser role has no usable model: {err}") from err
    if not chain or not chain[0][0].capabilities(chain[0][1]).vision:
        raise ToolError(
            "unsupported",
            "the browser role's model cannot see images",
            hint="set [roles] browser to a vision model, or call research without browser=true",
        )


# =====================================================================================
# BROWSER
# =====================================================================================


async def browser_of(ctx: Ctx) -> Browser:
    """This agent's browser, started on first use."""
    browser = ctx.state.browsers.get(ctx.agent_id)
    if browser is not None:
        return browser
    factory = ctx.state.browser_factory
    if factory is None:
        raise ToolError("unsupported", "no browser is available in this session")
    try:
        browser = await factory.new_browser()
    except BrowserError as err:
        raise ToolError("unsupported", str(err), hint=err.hint) from err
    ctx.state.browsers[ctx.agent_id] = browser
    return browser


async def close_browser(ctx: Ctx, agent_id: str) -> None:
    """Close an agent's browser when the agent is done."""
    browser = ctx.state.browsers.pop(agent_id, None)
    if browser is not None:
        await browser.close()


async def browse(ctx: Ctx, action: Callable[[Browser], Awaitable[PageView]]) -> ToolResult:
    """Run one browser action and show the page: title, URL and (within the limit) a screenshot."""
    browser = await browser_of(ctx)
    try:
        view = await action(browser)
    except BrowserError as err:
        raise ToolError("not_found" if "waiting for" in str(err) else "network", str(err)) from err
    taken = ctx.state.screenshots.get(ctx.agent_id, 0)
    lines = [f"title: {view.title}", f"url: {view.url}"]
    images = []
    if view.image is not None and taken < ctx.cfg.browser.max_screenshots:
        ctx.state.screenshots[ctx.agent_id] = taken + 1
        images.append(view.image)
        size = f"{ctx.cfg.browser.viewport_width}x{ctx.cfg.browser.viewport_height}"
        lines.append(f"screenshot attached ({size} px; click points use these coordinates)")
    elif view.image is not None:
        lines.append("no screenshot: the screenshot limit is reached; use browser_read")
    return ToolResult(call_id="", ok=True, text="\n".join(lines), images=images)


@tool(group="browser", permission="ask", read_only=True, specifier_arg="url")
async def browser_open(ctx: Ctx, url: Annotated[str, "Full http(s) URL."]) -> ToolResult:
    """Open a page in the browser and show it."""
    target = await checked_url(url)
    return await browse(ctx, lambda b: b.open(target))


@tool(group="browser", permission="auto", read_only=True)
async def browser_click(
    ctx: Ctx,
    target: Annotated[
        str, "Visible text of the element, `css=<selector>`, or `x,y` pixels from the screenshot."
    ],
) -> ToolResult:
    """Click an element and show the page afterwards."""
    return await browse(ctx, lambda b: b.click(target))


@tool(group="browser", permission="auto", read_only=True)
async def browser_type(
    ctx: Ctx,
    target: Annotated[str, "Label or placeholder of the field, or `css=<selector>`."],
    text: Annotated[str, "Text to enter (replaces the field's content)."],
    submit: Annotated[bool, "Press Enter afterwards."] = False,
) -> ToolResult:
    """Type into a form field and show the page afterwards."""
    return await browse(ctx, lambda b: b.type(target, text, submit))


@tool(group="browser", permission="auto", read_only=True)
async def browser_scroll(
    ctx: Ctx, pixels: Annotated[int, "Pixels to scroll: positive down, negative up."] = 700
) -> ToolResult:
    """Scroll the page and show it."""
    return await browse(ctx, lambda b: b.scroll(pixels))


@tool(group="browser", permission="auto", read_only=True)
async def browser_back(ctx: Ctx) -> ToolResult:
    """Go back to the previous page and show it."""
    return await browse(ctx, lambda b: b.back())


@tool(group="browser", permission="auto", read_only=True)
async def browser_screenshot(ctx: Ctx) -> ToolResult:
    """Show the current page again."""
    return await browse(ctx, lambda b: b.view())


@tool(group="browser", permission="auto", read_only=True)
async def browser_read(ctx: Ctx) -> str:
    """The visible text of the current page (cheaper than a screenshot for long text)."""
    browser = await browser_of(ctx)
    try:
        return await browser.read()
    except BrowserError as err:
        raise ToolError("network", str(err)) from err


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
BUILTIN_ROLES = frozenset(
    {"coder", "tester", "reviewer", "researcher", "browser", "explore", "lead"}
)


@tool(group="plan", permission="auto", read_only=True)
async def todo_write(
    ctx: Ctx,
    todos: Annotated[
        list[Todo],
        "The whole list, in order; it replaces the previous one. At most one item in_progress.",
    ],
) -> str:
    """Write your todo list for multi-step work; the user sees it live."""
    problems = todo_problems(todos)
    if problems:
        raise ToolError("invalid_args", "the todo list is not valid", body="\n".join(problems))
    ctx.state.todos[ctx.agent_id] = list(todos)
    await ctx.bus.publish(
        TodosUpdated(
            session_id=ctx.session.id, agent_id=ctx.agent_id, ts=time.time(), todos=list(todos)
        )
    )
    return "\n".join(todo_lines(todos))


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
    numbered = old.steps if old and ctx.role == "replanner" else []  # new ids never reuse old ones
    first = max((int(s.id[1:]) for s in numbered if s.id[1:].isdigit()), default=0) + 1
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


class StepUpdate(BaseModel):
    """A status change for one step."""

    step_id: str
    status: Literal["todo", "doing", "failed", "skipped"]  # "done" is not allowed here
    note: str = ""

    @field_validator("status", mode="before")
    @classmethod
    def _no_done(cls, value: object) -> object:
        if value == "done":
            raise ValueError(
                "'done' is not allowed here; use finish_step, which runs the step's check"
            )
        return value


ALLOWED_TRANSITIONS = {
    ("todo", "doing"),
    ("todo", "skipped"),
    ("doing", "todo"),
    ("doing", "failed"),
    ("doing", "skipped"),
    ("failed", "todo"),
}


@tool(group="plan", permission="auto", read_only=True)
async def update_plan(
    ctx: Ctx,
    updates: Annotated[list[StepUpdate], "Status changes, 1-30."],
    explanation: Annotated[str, "Short reason, max 200 chars."] = "",
) -> str:
    """Change step statuses. Use finish_step to mark a step done."""
    plan = ctx.session.plan
    if plan is None:
        raise ToolError("invalid_args", "there is no plan yet")
    require(1 <= len(updates) <= 30, "send 1 to 30 updates")
    require(len(explanation) <= 200, "explanation is limited to 200 characters")
    statuses = {s.id: s.status for s in plan.steps}
    for update in updates:
        if update.step_id not in statuses:
            raise ToolError("invalid_args", f"there is no step {update.step_id}")
        change = (statuses[update.step_id], update.status)
        if change not in ALLOWED_TRANSITIONS:
            raise ToolError(
                "invalid_args", f"{update.step_id} cannot go from {change[0]} to {change[1]}"
            )
        require(
            update.status != "skipped" or bool(update.note.strip()),
            f"skipping {update.step_id} needs a note",
        )
        statuses[update.step_id] = update.status
    require(sum(1 for v in statuses.values() if v == "doing") <= 1, "at most one step can be doing")
    for update in updates:
        step = plan.steps[[s.id for s in plan.steps].index(update.step_id)]
        step.status = update.status
        if update.note:
            step.notes = update.note
    await save_plan(ctx)
    return f"plan updated (version {plan.version})\n{checklist(plan)}"


@tool(group="plan", permission="auto", read_only=False)
async def finish_step(
    ctx: Ctx,
    step_id: Annotated[str, "The step you completed, e.g. 's3'."],
    summary: Annotated[str, "What you changed, max 2000 chars."],
    evidence: Annotated[str, "Commands you ran and their results, max 4000 chars."] = "",
) -> str:
    """Report a step as complete; Forge runs its check and only then marks it done."""
    plan = ctx.session.plan
    step = plan.step(step_id) if plan else None
    if plan is None or step is None:
        raise ToolError("invalid_args", f"there is no step {step_id}")
    if step.status != "doing":
        raise ToolError("invalid_args", f"{step_id} is {step.status}, not doing")
    result = await settle_step(ctx, step, summary[:2000])
    if result.passed:
        upcoming = plan.next_ready_step()
        after = f"next step: {upcoming.id} {upcoming.title}" if upcoming else "no steps are waiting"
        return f"step {step_id} done: check passed ({result.label})\n{after}"
    body = "--- check output ---\n" + (result.output or "(no output)")
    limit = ctx.cfg.limits.max_step_attempts
    if result.timed_out:
        raise ToolError("timeout", f"step {step_id} check took longer than 600s", body=body)
    if step_failed(step):  # settle_step changed the status
        raise ToolError(
            "limit_reached",
            f"step {step_id} check failed {limit} times; the step is marked failed",
            body=body,
        )
    raise ToolError(
        "check_failed",
        f"step {step_id} check failed (attempt {step.attempts} of {limit})",
        body=body,
    )


def step_failed(step: Step) -> bool:
    """True once a step has used up its attempts."""
    return step.status == "failed"


# =====================================================================================
# AGENTS
# =====================================================================================


@tool(group="agents", permission="auto", read_only=False, specifier_arg="role")
async def spawn_agent(
    ctx: Ctx,
    role: Annotated[
        str,
        "Role name: explore, coder, tester, reviewer, researcher, browser, or a custom agent.",
    ],
    task: Annotated[str, "Self-contained instructions: goal, relevant files, what to return."],
    background: Annotated[bool, "Run in parallel and get a message when done."] = False,
    isolation: Annotated[Literal["none", "worktree"], "Run in its own git worktree."] = "none",
    max_turns: Annotated[int, "Turn limit for the sub-agent (1-200)."] = 30,
    name: Annotated[str | None, "Optional name for messaging, e.g. 'api-tests'."] = None,
) -> str:
    """Start a sub-agent with its own context. It returns only its final report."""
    return await team_of(ctx).spawn(
        ctx,
        role,
        task,
        background=background,
        isolation=isolation,
        max_turns=max_turns,
        name=name,
    )


@tool(group="agents", permission="auto", read_only=True)
async def send_message(
    ctx: Ctx,
    to: Annotated[str, "Agent id or name, or 'main' for the lead."],
    text: Annotated[str, "The message, max 10,000 chars."],
    summary: Annotated[str, "Optional one-line preview, max 100 chars."] = "",
) -> str:
    """Send a message to another running agent."""
    return await team_of(ctx).send(ctx, to, text, summary)


@tool(group="agents", permission="auto", read_only=True)
async def list_agents(ctx: Ctx) -> str:
    """List all agents in this session with status and usage."""
    return team_of(ctx).overview(ctx)


@tool(group="agents", permission="auto", read_only=False)
async def stop_agent(
    ctx: Ctx,
    agent_id: Annotated[str, "Agent id or name."],
    keep_worktree: Annotated[bool, "Keep its worktree for inspection."] = True,
) -> str:
    """Cancel a running agent and its background jobs."""
    return await team_of(ctx).stop(ctx, agent_id, keep_worktree)


BoardStatus = Literal["ready", "blocked", "doing", "done", "failed", "skipped"]


@tool(group="agents", permission="auto", read_only=True)
async def read_board(
    ctx: Ctx,
    status: Annotated[list[BoardStatus] | None, "Only these statuses; default all."] = None,
) -> str:
    """Show the team's tasks with status, owner and dependencies."""
    return await team_of(ctx).read_board(ctx, list(status) if status else None)


@tool(group="agents", permission="auto", read_only=False)
async def claim_task(ctx: Ctx, task_id: Annotated[str, "Step id, e.g. 's3'."]) -> str:
    """Take a ready task so no other agent works on it."""
    return await team_of(ctx).claim_task(ctx, task_id)


@tool(group="agents", permission="auto", read_only=False)
async def update_task(
    ctx: Ctx,
    task_id: Annotated[str, "Step id you own."],
    status: Annotated[Literal["doing", "done", "failed"], "New status."],
    result: Annotated[str, "What you did, or why it failed (max 4000 chars)."],
) -> str:
    """Report progress or the result of your task to the lead."""
    return await team_of(ctx).update_task(ctx, task_id, status, result)


def team_of(ctx: Ctx) -> Team:
    """The session's agents; unsupported when the session has none."""
    if ctx.state.team is None:
        raise ToolError("unsupported", "agents are not available in this session")
    return ctx.state.team


# =====================================================================================
# MEMORY
# =====================================================================================

NOTES_HEADING = "## Notes from Forge"
MAX_NOTE_CHARS = 500


@tool(group="memory", permission="ask", read_only=False)
async def remember(
    ctx: Ctx,
    note: Annotated[str, "One fact or rule to keep for future sessions, max 500 chars."],
    scope: Annotated[
        Literal["project", "user"], "project = ./FORGE.md, user = ~/.forge/FORGE.md."
    ] = "project",
) -> str:
    """Save a lasting note, e.g. a build command or a coding convention."""
    note = " ".join(note.split())
    require(0 < len(note) <= MAX_NOTE_CHARS, "note must be 1-500 characters")
    target = ctx.root / "FORGE.md" if scope == "project" else forge_home() / "FORGE.md"
    old = decode_text(await read_bytes(target)) if target.is_file() else None
    text = with_note(old.text if old else "", note, datetime.date.today().isoformat())
    if text is None:
        return f'already remembered: "{note}"'
    data = encode_text(text, old)
    if scope == "project":
        await apply_changes(ctx, [FileChange(target, data)])
    else:
        # The user file lives outside the project's writable roots, so it is written directly.
        await asyncio.to_thread(_write_user_file, target, data)
    return f'remembered in {"FORGE.md" if scope == "project" else target}: "{note}"'


def with_note(text: str, note: str, today: str) -> str | None:
    """`text` with the note appended under the Forge heading; None if it is already there."""
    if any(line.startswith(f"- {note} (added ") for line in split_lines(text)):
        return None
    text = text or "# FORGE.md\n"
    if NOTES_HEADING not in split_lines(text):
        text = text.rstrip("\n") + f"\n\n{NOTES_HEADING}\n\n"
    elif not text.endswith("\n"):
        text += "\n"
    return text + f"- {note} (added {today})\n"


def _write_user_file(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


@tool(group="memory", permission="auto", read_only=True)
async def recall(
    ctx: Ctx,
    query: Annotated[str, "Words to search for in past sessions of this project."],
    limit: Annotated[int, "Number of results (1-20)."] = 5,
) -> str:
    """Search earlier sessions of this project: messages, summaries and plans."""
    require(query.strip() != "", "query must not be empty")
    require(1 <= limit <= 20, "limit must be between 1 and 20")
    hits = await ctx.store.search(str(ctx.root), query, limit + 1)
    hits = [(sid, text) for sid, text in hits if sid != ctx.session.id][:limit]
    if not hits:
        return f'no earlier sessions mention "{query}"'
    lines: list[str] = []
    for number, (session_id, text) in enumerate(hits, start=1):
        session = await ctx.store.load_session(session_id)
        day = datetime.date.fromtimestamp(session.created_at).isoformat()
        title = session.spec.goal if session.spec else ""
        lines.append(f'{number}. session {session_id[:8]} ({day}) "{title}"')
        lines.append("   ..." + " ".join(text.split()) + "...")
    return "\n".join(lines)


# =====================================================================================
# MCP
# =====================================================================================


@tool(group="mcp", permission="auto", read_only=True)
async def list_mcp_resources(
    ctx: Ctx, server: Annotated[str | None, "Only this server; default all."] = None
) -> str:
    """List resources offered by connected MCP servers."""
    lines = await mcp_of(ctx).list_resources(server)
    return "\n".join(lines) if lines else "no MCP resources available"


@tool(group="mcp", permission="auto", read_only=True)
async def read_mcp_resource(
    ctx: Ctx,
    server: Annotated[str, "Server name."],
    uri: Annotated[str, "Resource URI from list_mcp_resources."],
) -> ToolResult:
    """Read one resource from an MCP server."""
    return await mcp_of(ctx).read_resource(ctx, server, uri)


@tool(group="mcp", permission="auto", read_only=True)
async def tool_search(
    ctx: Ctx,
    query: Annotated[str, "Words describing the tool you need, e.g. 'create github issue'."],
    limit: Annotated[int, "Maximum tools to load (1-10)."] = 5,
) -> str:
    """Find and load MCP tools that are not yet in your tool list."""
    require(query.strip() != "", "query must not be empty")
    require(1 <= limit <= 10, "limit must be between 1 and 10")
    hub = mcp_of(ctx)
    if not hub.deferred:
        raise ToolError("unsupported", "all tools are loaded", hint="all tools are already loaded")
    found = hub.search(query, limit)
    if not found:
        servers = ", ".join(hub.servers())
        return f'no deferred tools match "{query}"; available servers: {servers}'
    lines = [f"loaded {len(found)} tools:"]
    lines += [f"- {t.name}: {first_line(t.spec.description)}" for t in found]
    return "\n".join(lines)


def mcp_of(ctx: Ctx) -> McpTools:
    """The session's MCP servers; unsupported when none are configured."""
    if ctx.state.mcp is None:
        raise ToolError(
            "unsupported", "no MCP servers are connected", hint="add [mcp_servers.<name>]"
        )
    return ctx.state.mcp
