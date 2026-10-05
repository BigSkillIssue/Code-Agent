"""The Python API: run Forge from your own code.

    forge = Forge()                       # config, store and executor from the project
    report = await forge.run("Fix the failing test")
    async for event in forge.stream("Add a --verbose flag"):
        print(event.kind)

Every port can be replaced: pass your own renderer (to answer questions and approvals),
store, or executor. Without a renderer the run is headless: questions take their defaults
and tool approvals follow `approve` (default: allow, as `forge run --yes` would).
"""

import asyncio
import time
from collections.abc import AsyncIterator
from pathlib import Path

from forge.config import ForgeConfig, find_project_root, load_config
from forge.ctx import Ctx
from forge.events import Event, SessionDone
from forge.local.auto_renderer import AutoRenderer
from forge.pipeline import Report, report_text, run_task
from forge.ports import Approval, Executor, Renderer, Store
from forge.providers.base import ToolCall
from forge.wiring import close_session, open_session, show_events

__all__ = ["Forge", "Report"]


class Forge:
    """A configured Forge you can give tasks to."""

    def __init__(
        self,
        config: ForgeConfig | None = None,
        renderer: Renderer | None = None,
        store: Store | None = None,
        executor: Executor | None = None,
        *,
        root: Path | str | None = None,
        approve: bool = True,
    ) -> None:
        self.root = find_project_root(Path(root) if root is not None else Path.cwd())
        self.config = config or load_config(self.root)
        self.headless = renderer is None
        self.renderer: Renderer = renderer or (AutoRenderer() if approve else RefusingRenderer())
        self.store = store
        self.executor = executor

    async def open(self) -> Ctx:
        """A new session with this Forge's ports (call close_session when done)."""
        return await open_session(
            self.root,
            self.config,
            self.renderer,
            store=self.store,
            executor=self.executor,
            headless=self.headless,
        )

    async def run(self, prompt: str) -> Report:
        """Work on one task and return the report; the renderer sees every event."""
        ctx = await self.open()
        shower = asyncio.create_task(show_events(ctx.bus.subscribe(ctx.session.id), self.renderer))
        try:
            report = await run_task(prompt, ctx)
            await publish_done(ctx, report)
            await asyncio.wait({shower}, timeout=5)
            return report
        finally:
            shower.cancel()
            await close_session(ctx)

    async def stream(self, prompt: str) -> AsyncIterator[Event]:
        """Work on one task, yielding every event; the last one is SessionDone."""
        ctx = await self.open()
        events = ctx.bus.subscribe(ctx.session.id)
        task = asyncio.create_task(run_and_finish(prompt, ctx))
        try:
            async for event in events:
                yield event
                if isinstance(event, SessionDone):
                    break
            await task  # re-raise a crash of the run
        finally:
            task.cancel()
            await close_session(ctx)


async def run_and_finish(prompt: str, ctx: Ctx) -> Report:
    """run_task, then the SessionDone event that ends a stream."""
    report = await run_task(prompt, ctx)
    await publish_done(ctx, report)
    return report


async def publish_done(ctx: Ctx, report: Report) -> None:
    """Tell renderers and stream readers that the session ended."""
    done = SessionDone(
        session_id=ctx.session.id, ts=time.time(), ok=report.ok, report=report_text(report)
    )
    await ctx.bus.publish(done)


class RefusingRenderer(AutoRenderer):
    """Headless renderer for approve=False: defaults for questions, no to every approval."""

    async def approve(self, call: ToolCall, reason: str) -> Approval:
        """Refuse; the model is told why."""
        return Approval(allow=False, feedback=f"{reason}; approvals are off for this run")
