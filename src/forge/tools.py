"""tools.py — every tool Forge can use, in one file.

Table of contents:
  FRAMEWORK  ToolDef, REGISTRY, tool(), make_tool_def(), for_role(), call_tool()

Each tool is a plain async function `fn(ctx, **args)` with a decorator. The decorator
reads the signature and docstring and generates the JSON schema, so a tool is written
once and never described twice.
"""

import asyncio
import inspect
import json
import logging
import re
import time
import typing
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Any, Literal, TypeVar, get_args, get_origin

from pydantic import BaseModel, ConfigDict, Field, ValidationError, create_model

from forge.config import ForgeConfig
from forge.ctx import Ctx
from forge.providers.base import ToolCall, ToolResult, ToolSpec
from forge.runtime.errors import ToolError, failure

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
