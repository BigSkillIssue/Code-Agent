"""Context compression: keep every request inside the model's window, in three levels.

1. Trim (every turn, no model call): old tool outputs are cut to head and tail; file reads
   superseded by a later read of the same file become a one-line stub.
2. Summarize (at `limits.compact_at` of the budget): everything but the recent turns is
   condensed by the `compressor` role into a note.
3. Reset (at `limits.reset_at`, or when level 2 is not enough): a fresh context of spec,
   plan, summary, unresolved errors, touched files and the task.

Only what is sent to the model changes; the transcript in the store stays complete. The plan
and unresolved errors are copied into every compacted context by code, not left to the model.
"""

import re
import time
from collections.abc import Sequence

from forge import prompts
from forge.ctx import Ctx
from forge.events import Compacted
from forge.modelcall import complete
from forge.plan import checklist
from forge.providers.base import (
    Capabilities,
    ChatRequest,
    Message,
    ProviderError,
    TextPart,
    ToolResult,
    ToolSpec,
    text_message,
)
from forge.providers.registry import resolve_role
from forge.providers.tokens import CHARS_PER_TOKEN, estimate_tokens, message_tokens

TRIM_AFTER = 6  # tool results newer than this many are never trimmed
TRIM_OVER_CHARS = 2_000
TRIM_KEEP_CHARS = 800  # head and tail kept of a trimmed output
RECENT_SHARE = 0.30  # level 2 keeps recent turns up to this share of the budget
SUMMARY_SHARE = 0.20  # a summary note may use at most this share of the budget
MAX_ERRORS = 5
ERROR_CHARS = 500
EDIT_TOOLS = ("write_file", "edit_file")
PATCH_PATH = re.compile(
    r"^\*\*\* (?:Add File|Update File|Delete File|Move to): (.+)$", re.MULTILINE
)
COMPACTED_TAG = "<compacted_context>"


def input_budget(ctx: Ctx, role: str) -> int:
    """Tokens a request may use: the window of the role's first model minus an output reserve."""
    try:
        chain = resolve_role(role, ctx.cfg)
    except ProviderError:
        chain = []
    caps = chain[0][0].capabilities(chain[0][1]) if chain else Capabilities()
    return caps.context_window - min(caps.max_output, caps.context_window // 8)


def request_tokens(system: str, messages: list[Message], tools: Sequence[ToolSpec]) -> int:
    """Estimated input tokens of one request."""
    return estimate_tokens(
        ChatRequest(model="", system=system, messages=messages, tools=list(tools))
    )


async def compact(
    ctx: Ctx,
    messages: list[Message],
    *,
    role: str,
    system: str,
    tools: Sequence[ToolSpec],
    task: str,
) -> list[Message]:
    """The messages to send next: trimmed, and summarized or reset when they grow too large."""
    messages = trim(messages)
    budget = input_budget(ctx, role)
    before = request_tokens(system, messages, tools)
    record_usage(ctx, system, messages, tools, budget)
    limits = ctx.cfg.limits
    level, ctx.state.compact_request = ctx.state.compact_request, 0
    if not level:
        level = (
            3
            if before >= limits.reset_at * budget
            else 2
            if before >= limits.compact_at * budget
            else 0
        )
    if not level:
        return messages
    await ctx.hooks.run("pre_compact", {"level": level, "tokens": before}, ctx)
    result = await summarize(ctx, messages, budget, task) if level == 2 else None
    if result is None or request_tokens(system, result, tools) >= limits.reset_at * budget:
        level, result = 3, await reset(ctx, messages, budget, task)
    after = request_tokens(system, result, tools)
    record_usage(ctx, system, result, tools, budget)
    await ctx.bus.publish(
        Compacted(
            session_id=ctx.session.id,
            agent_id=ctx.agent_id,
            ts=time.time(),
            level=level,
            tokens_before=before,
            tokens_after=after,
        )
    )
    return result


# ----------------------------------------------------------------------------- level 1


def trim(messages: list[Message]) -> list[Message]:
    """Cut old successful tool outputs and stub superseded reads (copies; inputs untouched)."""
    reads = read_paths(messages)
    latest = {path: call_id for call_id, path in reads.items()}  # later reads overwrite earlier
    results = [i for i, m in enumerate(messages) if m.tool_result is not None]
    old = set(results[:-TRIM_AFTER]) if len(results) > TRIM_AFTER else set()
    out: list[Message] = []
    for index, message in enumerate(messages):
        result = message.tool_result
        if result is None or not result.ok or index not in old:
            out.append(message)
            continue
        path = reads.get(result.call_id)
        if path is not None and latest[path] != result.call_id:
            text = f"[superseded: {path} was read again later]"
        elif len(result.text) > TRIM_OVER_CHARS:
            text = head_and_tail(result.text)
        else:
            out.append(message)
            continue
        out.append(
            message.model_copy(update={"tool_result": result.model_copy(update={"text": text})})
        )
    return out


def head_and_tail(text: str) -> str:
    """The first and last part of a long output."""
    cut = len(text) - 2 * TRIM_KEEP_CHARS
    head, tail = text[:TRIM_KEEP_CHARS], text[-TRIM_KEEP_CHARS:]
    return f"{head}\n[... {cut} chars trimmed ...]\n{tail}"


def read_paths(messages: list[Message]) -> dict[str, str]:
    """call id -> path for every full read_file call (in order)."""
    paths: dict[str, str] = {}
    for message in messages:
        for call in message.tool_calls:
            if (
                call.name == "read_file"
                and "offset" not in call.arguments
                and "path" in call.arguments
            ):
                paths[call.id] = str(call.arguments["path"])
    return paths


# ----------------------------------------------------------------------------- level 2 and 3


async def summarize(
    ctx: Ctx, messages: list[Message], budget: int, task: str
) -> list[Message] | None:
    """Summary of the older messages followed by the recent turns; None if nothing is old."""
    split = recent_start(messages, int(budget * RECENT_SHARE))
    if split == 0:
        return None
    old, recent = messages[:split], messages[split:]
    note = await summary_note(ctx, old, budget)
    errors = unresolved_errors(messages)
    first = compacted_message(ctx, note, errors, touched_files(old), task)
    return [first, *recent]


async def reset(ctx: Ctx, messages: list[Message], budget: int, task: str) -> list[Message]:
    """A fresh context: spec, plan, summary of everything, unresolved errors, files, task."""
    note = await summary_note(ctx, messages, budget)
    return [
        compacted_message(ctx, note, unresolved_errors(messages), touched_files(messages), task)
    ]


def recent_start(messages: list[Message], keep_tokens: int) -> int:
    """Index where the kept recent turns begin: an assistant message, within `keep_tokens`."""
    used = 0
    start = len(messages)
    for index in range(len(messages) - 1, 0, -1):
        used += message_tokens(messages[index])
        if used > keep_tokens:
            break
        if messages[index].role == "assistant":
            start = index  # never begin with a tool result whose call would be summarized away
    return start


async def summary_note(ctx: Ctx, messages: list[Message], budget: int) -> str:
    """The compressor's note on `messages`, capped to its share of the budget."""
    limit = int(budget * SUMMARY_SHARE * CHARS_PER_TOKEN)
    text = transcript(messages, compressor_chars(ctx))
    system = prompts.render("compressor", transcript=text)
    try:
        reply, _ = await complete(
            ctx, "compressor", system, [text_message("user", prompts.render("compress_task"))]
        )
        note = reply.text().strip()
    except ProviderError:
        note = ""  # without a note the plan, errors and files below still carry the work on
    note = note or ctx.session.summary
    ctx.session.summary = note[:limit]
    return ctx.session.summary


def compressor_chars(ctx: Ctx) -> int:
    """How much transcript the compressor's own window can take."""
    return max(int(input_budget(ctx, "compressor") * 0.8) * CHARS_PER_TOKEN, 4_000)


def transcript(messages: list[Message], max_chars: int) -> str:
    """Messages as plain text for the compressor; the newest part wins when it is too long."""
    lines: list[str] = []
    for message in messages:
        if message.tool_result is not None:
            status = "ok" if message.tool_result.ok else "failed"
            lines.append(f"[tool result, {status}] {message.tool_result.text}")
            continue
        calls = "; ".join(f"{c.name}({c.arguments})" for c in message.tool_calls)
        lines.append(
            f"[{message.role}] {message.text()}" + (f"\n[tool calls] {calls}" if calls else "")
        )
    text = "\n\n".join(lines)
    return text if len(text) <= max_chars else "[... earlier part cut ...]\n" + text[-max_chars:]


def unresolved_errors(messages: list[Message]) -> list[str]:
    """The latest failure of each tool that did not succeed afterwards (newest last)."""
    names = {c.id: c.name for m in messages for c in m.tool_calls}
    errors: dict[str, str] = {}
    for message in messages:
        result: ToolResult | None = message.tool_result
        if result is None:
            continue
        name = names.get(result.call_id, "tool")
        errors.pop(name, None)
        if not result.ok:
            errors[name] = f"{name}: {result.text[:ERROR_CHARS]}"
    return list(errors.values())[-MAX_ERRORS:]


def touched_files(messages: list[Message]) -> list[str]:
    """Paths written by edit tools or patches, including those listed by earlier compactions."""
    found: list[str] = []
    for message in messages:
        if message.role == "user" and message.text().startswith(COMPACTED_TAG):
            block = re.search(
                r"<files_touched>\n(.*?)\n</files_touched>", message.text(), re.DOTALL
            )
            found += block.group(1).splitlines() if block else []
        for call in message.tool_calls:
            if call.name in EDIT_TOOLS and "path" in call.arguments:
                found.append(str(call.arguments["path"]))
            elif call.name == "apply_patch":
                found += PATCH_PATH.findall(str(call.arguments.get("patch", "")))
    return list(dict.fromkeys(p.strip() for p in found if p.strip()))


def compacted_message(
    ctx: Ctx, note: str, errors: list[str], files: list[str], task: str
) -> Message:
    """The user message that stands in for the compacted history (data, in tagged sections)."""
    session = ctx.session
    sections = [
        ("spec", session.spec.model_dump_json(indent=1) if session.spec else ""),
        ("plan", checklist(session.plan) if session.plan else ""),
        ("summary", note),
        (
            "unresolved_errors",
            "\n".join([*errors, *([ctx.state.failure] if ctx.state.failure else [])]),
        ),
        ("files_touched", "\n".join(files)),
        ("task", task),
    ]
    body = "\n".join(f"<{name}>\n{text}\n</{name}>" for name, text in sections if text)
    return Message(
        role="user", parts=[TextPart(text=f"{COMPACTED_TAG}\n{body}\n</compacted_context>")]
    )


def record_usage(
    ctx: Ctx, system: str, messages: list[Message], tools: Sequence[ToolSpec], budget: int
) -> None:
    """Remember the size of the latest request by category, for /context."""
    usage = {"system": len(system) // CHARS_PER_TOKEN, "tools": request_tokens("", [], tools)}
    for message in messages:
        key = "tool results" if message.tool_result else message.role
        usage[key] = usage.get(key, 0) + message_tokens(message)
    usage["total"] = request_tokens(system, messages, tools)
    usage["budget"] = budget
    ctx.state.context_usage = usage
