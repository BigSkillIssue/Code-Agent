"""Prompt-based tool calling for models without native tools.

The tool schemas go into the system prompt (`TOOL_FALLBACK`), the model answers with a fenced
JSON block, and this module turns that block into ordinary `ToolCall`s, so the agent loop
cannot tell the difference. History is rewritten the other way: earlier tool calls become JSON
text and tool results become user messages.
"""

import json
import re
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

from forge import prompts
from forge.providers.base import (
    Capabilities,
    ChatRequest,
    Message,
    Provider,
    StreamItem,
    TextPart,
    ToolCall,
    Usage,
    text_message,
)

FENCE = re.compile(r"```(?:json)?\s*\n(.*?)```", re.DOTALL)


@dataclass
class ParsedReply:
    """A model reply split into prose and tool calls; `error` is set when the JSON is unusable."""

    text: str
    calls: list[ToolCall] = field(default_factory=list)
    error: str | None = None


def parse_tool_calls(reply: str) -> ParsedReply:
    """Find the fenced `{"tool_calls": [...]}` block in a reply and parse it."""
    blocks = [b for b in FENCE.findall(reply) if "tool_calls" in b]
    if not blocks:
        return ParsedReply(text=reply.strip())
    prose = reply[: reply.find("```")].strip()
    try:
        data = json.loads(blocks[-1])
    except json.JSONDecodeError as exc:
        return ParsedReply(text=prose, error=f"the tool call JSON is invalid: {exc}")
    calls = data.get("tool_calls") if isinstance(data, dict) else None
    if not isinstance(calls, list) or not calls:
        return ParsedReply(
            text=prose, error='expected {"tool_calls": [...]} with at least one call'
        )
    parsed: list[ToolCall] = []
    for n, call in enumerate(calls):
        if not isinstance(call, dict) or not isinstance(call.get("name"), str):
            return ParsedReply(text=prose, error=f"tool call {n + 1} has no tool name")
        arguments = call.get("arguments") or {}
        if not isinstance(arguments, dict):
            return ParsedReply(text=prose, error=f"tool call {n + 1}: arguments must be an object")
        parsed.append(ToolCall(id=f"call_{n}", name=call["name"], arguments=arguments))
    return ParsedReply(text=prose, calls=parsed)


def fallback_request(req: ChatRequest) -> ChatRequest:
    """The same request without native tools: schemas in the system prompt, history as text."""
    schemas = [t.model_dump() for t in req.tools]
    instructions = prompts.render(
        "tool_fallback", model=req.model, tools=json.dumps(schemas, indent=1)
    )
    system = f"{req.system}\n\n{instructions}" if req.system else instructions
    return req.model_copy(
        update={"system": system, "tools": [], "messages": as_text_history(req.messages)}
    )


def as_text_history(messages: list[Message]) -> list[Message]:
    """Tool calls become JSON blocks; consecutive tool results become one user message."""
    out: list[Message] = []
    results: list[dict[str, Any]] = []
    for message in messages:
        if message.role == "tool" and message.tool_result:
            r = message.tool_result
            results.append({"call_id": r.call_id, "ok": r.ok, "output": r.text})
            continue
        if results:
            out.append(results_message(results))
            results = []
        if message.role == "assistant" and message.tool_calls:
            out.append(Message(role="assistant", parts=[TextPart(text=calls_text(message))]))
        else:
            out.append(message)
    if results:
        out.append(results_message(results))
    return out


def calls_text(message: Message) -> str:
    """An assistant turn with its tool calls written the way the fallback prompt asks."""
    calls = [{"name": c.name, "arguments": c.arguments} for c in message.tool_calls]
    block = "```json\n" + json.dumps({"tool_calls": calls}) + "\n```"
    return f"{message.text()}\n{block}".strip()


def results_message(results: list[dict[str, Any]]) -> Message:
    """Tool results as a user message (data only, no instructions)."""
    return text_message(
        "user", "```json\n" + json.dumps({"tool_results": results}, indent=1) + "\n```"
    )


class ToolFallbackProvider:
    """Wraps a provider whose model has no native tool calling."""

    def __init__(self, inner: Provider) -> None:
        self.inner = inner
        self.name = inner.name

    def capabilities(self, model: str) -> Capabilities:
        """The inner provider's capabilities."""
        return self.inner.capabilities(model)

    async def count_tokens(self, req: ChatRequest) -> int:
        """Tokens of the rewritten request."""
        return await self.inner.count_tokens(fallback_request(req))

    async def stream(self, req: ChatRequest) -> AsyncIterator[StreamItem]:
        """Stream the reply and parse tool calls; one corrective turn if the JSON is unusable."""
        request = fallback_request(req)
        total = Usage()
        for attempt in range(2):
            reply: Message | None = None
            async for item in self.inner.stream(request):
                if item.delta:
                    yield StreamItem(delta=item.delta)
                if item.done is not None:
                    reply = item.done
                    total += item.usage or Usage()
            text = reply.text() if reply else ""
            parsed = parse_tool_calls(text)
            if parsed.error is None or attempt == 1:
                yield StreamItem(done=message_of(parsed, text), usage=total)
                return
            fix = prompts.render("fix_json", model=req.model, failure=parsed.error)
            history = [
                *request.messages,
                text_message("assistant", text),
                text_message("user", fix),
            ]
            request = request.model_copy(update={"messages": history})


def message_of(parsed: ParsedReply, raw: str) -> Message:
    """The assistant message the agent loop sees; unusable JSON is left as plain text."""
    text = raw.strip() if parsed.error else parsed.text
    return Message(
        role="assistant", parts=[TextPart(text=text)] if text else [], tool_calls=parsed.calls
    )


def with_tool_fallback(provider: Provider, req: ChatRequest) -> Provider:
    """`provider` itself, or a fallback wrapper when the request has tools the model cannot call."""
    if req.tools and not provider.capabilities(req.model).tools:
        return ToolFallbackProvider(provider)
    return provider
