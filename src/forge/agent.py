"""The agent loop: call the model, run the tools it asks for, repeat until it is done."""

import asyncio
import datetime
import platform
import time
from typing import Literal

from pydantic import BaseModel

from forge import prompts
from forge.ctx import Ctx
from forge.events import ErrorEvent, ModelDelta, ModelDone, ToolFinished, ToolStarted
from forge.memory import load_memory, render_memory
from forge.providers.base import (
    ChatRequest,
    Message,
    Provider,
    ProviderError,
    ToolCall,
    ToolResult,
    Usage,
    text_message,
)
from forge.providers.registry import resolve_role
from forge.runtime.shell import find_shell
from forge.tools import REGISTRY, ToolDef, call_tool, for_role

ROLE_PROMPTS = {"coder": "coder"}


class AgentResult(BaseModel):
    """How one agent run ended."""

    text: str  # final assistant text
    messages: list[Message]  # this agent's transcript
    usage: Usage
    stopped: Literal["done", "max_turns", "budget", "cancelled", "error"]


async def run_agent(
    ctx: Ctx,
    task: str,
    *,
    role: str = "coder",
    history: list[Message] | None = None,
    max_turns: int = 40,
) -> AgentResult:
    """Loop: build request (prompts.render(role)), stream via the role's fallback chain,
    run tool calls with call_tool (parallel only if the model asked for several and
    all are read_only), append results, compact if needed, repeat until the model
    answers without tool calls or a limit is hit."""
    messages = [*(history or []), text_message("user", task)]
    _record(ctx, messages[-1])
    system = prompts.render(ROLE_PROMPTS.get(role, "coder"), **prompt_slots(ctx))
    tools = for_role(role, ctx.cfg)
    usage, text = Usage(), ""
    for _ in range(max_turns):
        messages = await compact_if_needed(ctx, messages)
        try:
            reply, turn_usage = await model_turn(ctx, role, system, messages, tools)
        except ProviderError as err:
            return AgentResult(text=str(err), messages=messages, usage=usage, stopped="error")
        usage += turn_usage
        messages.append(reply)
        _record(ctx, reply)
        text = reply.text() or text
        if not reply.tool_calls:
            return AgentResult(text=text, messages=messages, usage=usage, stopped="done")
        if usage.cost_usd > ctx.cfg.limits.max_cost_usd:
            return AgentResult(text=text, messages=messages, usage=usage, stopped="budget")
        for result in await run_tool_calls(ctx, reply.tool_calls):
            messages.append(Message(role="tool", tool_result=result))
            _record(ctx, messages[-1])
    return AgentResult(text=text, messages=messages, usage=usage, stopped="max_turns")


def prompt_slots(ctx: Ctx) -> dict[str, str]:
    """Values for the prompt slots that describe where the agent is working."""
    shells = [kind for kind in ("bash", "powershell") if find_shell(kind)]
    return {
        "cwd": str(ctx.cwd),
        "os": f"{platform.system()} {platform.release()}",
        "shell": " and ".join(shells) or "none",
        "date": datetime.date.today().isoformat(),
        "memory": render_memory(load_memory(ctx.root, ctx.cwd), ctx.root),
    }


async def compact_if_needed(ctx: Ctx, messages: list[Message]) -> list[Message]:
    """Hook point for context compression (S27); returns the messages unchanged for now."""
    return messages


async def model_turn(
    ctx: Ctx, role: str, system: str, messages: list[Message], tools: list[ToolDef]
) -> tuple[Message, Usage]:
    """One model answer, trying each model of the role's fallback chain in order."""
    chain = resolve_role(role, ctx.cfg)
    if not chain:
        raise ProviderError("bad_request", f"no model is configured for role '{role}'")
    specs = [t.spec for t in tools]
    last_error: ProviderError | None = None
    for provider, model in chain:
        request = ChatRequest(model=model, system=system, messages=messages, tools=specs)
        try:
            return await stream_reply(ctx, provider, request)
        except ProviderError as err:
            last_error = err
            await publish_error(ctx, f"{provider.name}/{model} failed ({err.kind}): {err}")
    assert last_error is not None
    raise last_error


async def stream_reply(ctx: Ctx, provider: Provider, request: ChatRequest) -> tuple[Message, Usage]:
    """Stream one reply, publishing text deltas and the finished message."""
    async for item in provider.stream(request):
        if item.delta:
            await ctx.bus.publish(
                ModelDelta(
                    session_id=ctx.session.id,
                    agent_id=ctx.agent_id,
                    ts=time.time(),
                    text=item.delta,
                )
            )
        if item.done is not None:
            usage = item.usage or Usage()
            await ctx.bus.publish(
                ModelDone(
                    session_id=ctx.session.id,
                    agent_id=ctx.agent_id,
                    ts=time.time(),
                    message=item.done,
                    usage=usage,
                )
            )
            return item.done, usage
    raise ProviderError("network", f"{provider.name} ended the stream without a final message")


async def run_tool_calls(ctx: Ctx, calls: list[ToolCall]) -> list[ToolResult]:
    """Run the calls of one turn: together if all are read-only, else one after another."""
    defs = [REGISTRY.get(call.name) for call in calls]
    if len(calls) > 1 and all(d is not None and d.read_only for d in defs):
        return list(await asyncio.gather(*(run_one_tool(ctx, call) for call in calls)))
    return [await run_one_tool(ctx, call) for call in calls]


async def run_one_tool(ctx: Ctx, call: ToolCall) -> ToolResult:
    """Run one tool call between its start and finish events."""
    now = time.time
    await ctx.bus.publish(
        ToolStarted(session_id=ctx.session.id, agent_id=ctx.agent_id, ts=now(), call=call)
    )
    result = await call_tool(ctx, call)
    await ctx.bus.publish(
        ToolFinished(session_id=ctx.session.id, agent_id=ctx.agent_id, ts=now(), result=result)
    )
    return result


async def publish_error(ctx: Ctx, message: str) -> None:
    """Tell the user about a problem that did not stop the agent."""
    await ctx.bus.publish(
        ErrorEvent(
            session_id=ctx.session.id, agent_id=ctx.agent_id, ts=time.time(), message=message
        )
    )


def _record(ctx: Ctx, message: Message) -> None:
    # The session keeps the full transcript of the main agent; sub-agents keep their own.
    if ctx.agent_id == "main":
        ctx.session.messages.append(message)
