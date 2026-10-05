"""The agent loop: call the model, run the tools it asks for, repeat until it is done."""

import asyncio
import datetime
import platform
import time
from typing import Literal

from pydantic import BaseModel

from forge import prompts
from forge.compress import compact
from forge.ctx import Ctx
from forge.events import ToolFinished, ToolStarted
from forge.memory import load_memory, render_memory
from forge.modelcall import complete, model_turn, publish_error
from forge.plan import checklist
from forge.providers.base import (
    Message,
    ProviderError,
    ToolCall,
    ToolResult,
    Usage,
    text_message,
)
from forge.runtime.shell import find_shell
from forge.skills import skills_listing
from forge.tools import REGISTRY, agent_tools, call_tool

__all__ = ["AgentResult", "complete", "over_budget", "publish_error", "run_agent"]

ROLE_PROMPTS = {"coder": "coder", "planner": "planner", "replanner": "replanner"}


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
    system = prompts.render(prompt_for(ctx, role), **prompt_slots(ctx))
    usage, text = Usage(), ""
    team = ctx.state.team
    for _ in range(max_turns):
        if over_budget(ctx):
            return AgentResult(text=text, messages=messages, usage=usage, stopped="budget")
        if team is not None:
            messages += deliver(ctx, team.take_messages(ctx.agent_id))
        specs = [t.spec for t in agent_tools(ctx, role)]  # tool_search can add tools
        messages = await compact(ctx, messages, role=role, system=system, tools=specs, task=task)
        try:
            reply, turn_usage = await model_turn(ctx, role, system, messages, specs)
        except ProviderError as err:
            return AgentResult(text=str(err), messages=messages, usage=usage, stopped="error")
        usage += turn_usage
        if team is not None:
            team.record_turn(ctx.agent_id, turn_usage)
        messages.append(reply)
        _record(ctx, reply)
        text = reply.text() or text
        if not reply.tool_calls:
            if team is not None and team.has_running_children(ctx.agent_id):
                # Reports of background agents are still due; wait and let the model use them.
                messages += deliver(ctx, await team.wait_for_message(ctx.agent_id))
                continue
            return AgentResult(text=text, messages=messages, usage=usage, stopped="done")
        if over_budget(ctx):
            return AgentResult(text=text, messages=messages, usage=usage, stopped="budget")
        for result in await run_tool_calls(ctx, reply.tool_calls):
            messages.append(Message(role="tool", tool_result=result))
            _record(ctx, messages[-1])
    return AgentResult(text=text, messages=messages, usage=usage, stopped="max_turns")


def prompt_for(ctx: Ctx, role: str) -> str:
    """The prompt name: sub-agents get the team-member (or explore) prompt."""
    if role in ctx.state.custom_roles:
        return "custom_agent"
    if ctx.agent_id != "main":
        return "explore" if role == "explore" else "team_member"
    if role == "coder" and ctx.state.mode in ("subagents", "team"):
        return "team_lead"
    return ROLE_PROMPTS.get(role, "coder")


def skills_text(ctx: Ctx) -> str:
    """The skills list for the system prompt (empty when there are none)."""
    listing = skills_listing(ctx.root)
    return prompts.render("skills", skills="\n".join(listing)) if listing else ""


def deferred_tools_text(ctx: Ctx) -> str:
    """The deferred MCP tools as name lines (empty when every tool is loaded)."""
    listing = ctx.state.mcp.deferred_listing() if ctx.state.mcp is not None else []
    return prompts.render("deferred_tools", tools="\n".join(listing)) if listing else ""


def over_budget(ctx: Ctx) -> bool:
    """True once the whole session (every agent) has spent its cost budget."""
    return ctx.state.usage.cost_usd >= ctx.cfg.limits.max_cost_usd


def prompt_slots(ctx: Ctx) -> dict[str, str]:
    """Values for the prompt slots that describe where the agent is working."""
    shells = [kind for kind in ("bash", "powershell") if find_shell(kind)]
    return {
        "cwd": str(ctx.cwd),
        "os": f"{platform.system()} {platform.release()}",
        "shell": " and ".join(shells) or "none",
        "date": datetime.date.today().isoformat(),
        "memory": render_memory(load_memory(ctx.root, ctx.cwd), ctx.root),
        "spec": ctx.session.spec.model_dump_json(indent=2) if ctx.session.spec else "(none)",
        "plan": checklist(ctx.session.plan) if ctx.session.plan else "(none)",
        "failure": ctx.state.failure or "(none)",
        "role": ctx.role,
        "deferred_tools": deferred_tools_text(ctx),
        "skills": skills_text(ctx),
        "agent_prompt": custom.prompt if (custom := ctx.state.custom_roles.get(ctx.role)) else "",
    }


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


def deliver(ctx: Ctx, texts: list[str]) -> list[Message]:
    """Inbox messages as user messages (recorded in the transcript)."""
    delivered = [text_message("user", text) for text in texts]
    for message in delivered:
        _record(ctx, message)
    return delivered


def _record(ctx: Ctx, message: Message) -> None:
    # The session keeps the full transcript of the main agent; sub-agents keep their own.
    if ctx.agent_id == "main":
        ctx.session.messages.append(message)
