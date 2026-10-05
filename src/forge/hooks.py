"""Hooks: user code that runs on events such as `pre_tool`, and may block them.

Shell hooks come from `[hooks]` in the config; Python hooks register with `@hook(...)`.
A hook's `match` is a regex on the tool name (tool events only; empty = every tool).
Exit code / outcome: 0 continue, 2 (or `HookOutcome(block=True)`) block with a message for
the model, anything else is a warning. A hanging hook is stopped after 30 s.
"""

import inspect
import re
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from forge.config import ForgeConfig, HookEventName
from forge.events import ErrorEvent
from forge.runtime.hook_runner import HOOK_TIMEOUT_S, run_hook

HookEvent = HookEventName
PythonHookFn = Callable[
    [str, dict[str, Any], Any], "HookOutcome | Awaitable[HookOutcome | None] | None"
]


class HookOutcome(BaseModel):
    """What the hooks for one event decided."""

    block: bool = False
    message: str = ""  # shown to the model when blocking


@dataclass(frozen=True)
class PythonHook:
    """A Python function registered with @hook."""

    event: str
    match: str
    fn: PythonHookFn


PYTHON_HOOKS: list[PythonHook] = []


def hook(event: HookEvent, match: str = "") -> Callable[[PythonHookFn], PythonHookFn]:
    """Register `fn(event, payload, ctx)` for an event; return HookOutcome(block=True) to block."""

    def register(fn: PythonHookFn) -> PythonHookFn:
        PYTHON_HOOKS.append(PythonHook(event, match, fn))
        return fn

    return register


def matches(pattern: str, payload: Mapping[str, Any]) -> bool:
    """Tool events match by tool name; other events ignore the pattern."""
    tool = payload.get("tool")
    if not pattern or not isinstance(tool, str):
        return True
    return re.fullmatch(pattern, tool) is not None


class Hooks:
    """Runs the configured and registered hooks of a session."""

    def __init__(self, cfg: ForgeConfig, python_hooks: list[PythonHook] | None = None) -> None:
        self.cfg = cfg
        self.python_hooks = PYTHON_HOOKS if python_hooks is None else python_hooks
        self.timeout_s = HOOK_TIMEOUT_S

    async def run(
        self, event: HookEvent, payload: Mapping[str, Any], ctx: Any = None
    ) -> HookOutcome:
        """Run every hook for `event`; the first one that blocks decides."""
        data = {"event": event, **payload}
        for spec in self.cfg.hooks.get(event, []):
            if matches(spec.match, data):
                outcome = await self.run_shell(spec.command, data, ctx)
                if outcome.block:
                    return outcome
        for python_hook in [h for h in self.python_hooks if h.event == event]:
            if matches(python_hook.match, data):
                outcome = await self.run_python(python_hook, data, ctx)
                if outcome.block:
                    return outcome
        return HookOutcome()

    async def run_shell(self, command: str, data: dict[str, Any], ctx: Any) -> HookOutcome:
        """One shell hook."""
        cwd = Path(getattr(ctx, "cwd", None) or Path.cwd())
        result = await run_hook(command, data, cwd, self.timeout_s)
        if result.code == 2:
            return HookOutcome(
                block=True, message=result.stderr.strip() or f"blocked by hook `{command}`"
            )
        if result.code != 0:
            detail = result.stderr.strip()[-300:] or f"exit code {result.code}"
            await warn(ctx, f"{data['event']} hook `{command}` failed: {detail}")
        return HookOutcome()

    async def run_python(
        self, python_hook: PythonHook, data: dict[str, Any], ctx: Any
    ) -> HookOutcome:
        """One Python hook; an exception is a warning, not a crash."""
        try:
            value = python_hook.fn(str(data["event"]), data, ctx)
            if inspect.isawaitable(value):
                value = await value
        except Exception as exc:
            await warn(ctx, f"{data['event']} hook {python_hook.fn.__name__} failed: {exc}")
            return HookOutcome()
        return value if isinstance(value, HookOutcome) else HookOutcome()


async def warn(ctx: Any, message: str) -> None:
    """Tell the user about a hook problem (the session goes on)."""
    if ctx is None:
        return
    await ctx.bus.publish(
        ErrorEvent(
            session_id=ctx.session.id, agent_id=ctx.agent_id, ts=time.time(), message=message
        )
    )
