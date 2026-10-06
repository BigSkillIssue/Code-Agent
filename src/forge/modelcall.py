"""Single model calls: stream one answer through a role's fallback chain, publishing events."""

import time
from collections.abc import Callable
from typing import Any

from forge.ctx import Ctx
from forge.events import ErrorEvent, ModelDelta, ModelDone
from forge.providers.base import (
    ChatRequest,
    Message,
    Provider,
    ProviderError,
    ToolCall,
    ToolSpec,
    Usage,
)
from forge.providers.fallback_tools import with_tool_fallback
from forge.providers.registry import resolve_role
from forge.providers.retry import MAX_RETRY_AFTER_S


async def complete(
    ctx: Ctx,
    role: str,
    system: str,
    messages: list[Message],
    json_schema: dict[str, Any] | None = None,
) -> tuple[Message, Usage]:
    """One tool-free model answer (refine, review, compress), with the role's fallback chain."""
    return await model_turn(ctx, role, system, messages, [], json_schema=json_schema)


async def model_turn(
    ctx: Ctx,
    role: str,
    system: str,
    messages: list[Message],
    tools: list[ToolSpec],
    json_schema: dict[str, Any] | None = None,
    on_tool_call: Callable[[ToolCall], None] | None = None,
) -> tuple[Message, Usage]:
    """One model answer, trying each model of the role's fallback chain in order."""
    chain = resolve_role(role, ctx.cfg)
    if not chain:
        raise ProviderError("bad_request", f"no model is configured for role '{role}'")
    specs = list(tools)
    last_error: ProviderError | None = None
    for provider, model in usable(ctx, chain):
        request = ChatRequest(
            model=model, system=system, messages=messages, tools=specs, json_schema=json_schema
        )
        try:
            return await stream_reply(
                ctx, with_tool_fallback(provider, request), request, on_tool_call
            )
        except ProviderError as err:
            last_error = err
            await publish_error(ctx, f"{provider.name}/{model} failed ({err.kind}): {err}")
            cool_down(ctx, f"{provider.name}/{model}", err)
    assert last_error is not None
    raise last_error


def usable(ctx: Ctx, chain: list[tuple[Provider, str]]) -> list[tuple[Provider, str]]:
    """The chain without models that are out of quota for a while (all of it if every one is)."""
    now = time.monotonic()
    ready = [(p, m) for p, m in chain if ctx.state.cooldowns.get(f"{p.name}/{m}", 0) <= now]
    return ready or chain


def cool_down(ctx: Ctx, key: str, err: ProviderError) -> None:
    """Skip a model whose quota is gone for longer than a retry would wait (daily limits)."""
    if err.kind == "rate_limit" and (err.retry_after_s or 0) > MAX_RETRY_AFTER_S:
        ctx.state.cooldowns[key] = time.monotonic() + (err.retry_after_s or 0)


async def stream_reply(
    ctx: Ctx,
    provider: Provider,
    request: ChatRequest,
    on_tool_call: Callable[[ToolCall], None] | None = None,
) -> tuple[Message, Usage]:
    """Stream one reply, publishing text deltas and the finished message."""
    async for item in provider.stream(request):
        if item.tool_call is not None and on_tool_call is not None:
            on_tool_call(item.tool_call)
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
            ctx.state.usage += usage
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


async def publish_error(ctx: Ctx, message: str) -> None:
    """Tell the user about a problem that did not stop the agent."""
    await ctx.bus.publish(
        ErrorEvent(
            session_id=ctx.session.id, agent_id=ctx.agent_id, ts=time.time(), message=message
        )
    )
