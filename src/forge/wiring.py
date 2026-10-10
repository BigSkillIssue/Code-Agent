"""Wire the local port implementations into a Ctx for one session (used by cli, tui and api)."""

import shutil
import sys
from collections.abc import AsyncIterator
from pathlib import Path

from forge.config import ForgeConfig, forge_home
from forge.ctx import Ctx
from forge.events import Event
from forge.hooks import Hooks
from forge.local.local_executor import LocalExecutor
from forge.local.memory_bus import MemoryBus
from forge.local.playwright_browser import PlaywrightBrowsers
from forge.local.sqlite_store import SqliteStore
from forge.local.xcode_builder import XcodeBuilder
from forge.mcp_client import McpHub
from forge.modelcall import publish_error
from forge.ports import EventBus, Executor, Renderer, Session, Store
from forge.providers.fake import FakeProvider
from forge.providers.registry import register_provider
from forge.runtime.ledger import ReadLedger
from forge.runtime.permissions import Permissions
from forge.team import AgentRegistry

FAKE_FIXTURE = Path("tests") / "fixtures" / "fake" / "hello.json"
BUILTIN_FAKE = {
    "roles": {
        "refiner": [
            {
                "text": '{"goal": "Say hello", "context": "", "requirements": [], '
                '"acceptance_criteria": ["a greeting is printed"], "size": "trivial"}'
            }
        ]
    },
    "turns": [{"text": "Hello from the fake provider."}],
}
FAKE_ROLES = (
    "refiner",
    "planner",
    "coder",
    "reviewer",
    "compressor",
    "explore",
    "tester",
    "researcher",
    "browser",
    "lead",
)


def use_fake_provider(cfg: ForgeConfig, script: Path | None, project_root: Path) -> FakeProvider:
    """Send every role to a FakeProvider replaying `script` (default: the hello fixture)."""
    path = script or project_root / FAKE_FIXTURE
    fake = FakeProvider.from_file(path) if path.is_file() else FakeProvider.from_data(BUILTIN_FAKE)
    return install_fake(cfg, fake)


def install_fake(cfg: ForgeConfig, fake: FakeProvider) -> FakeProvider:
    """Route every role to `fake`, using the role name as the model name."""
    register_provider(cfg, fake)
    cfg.roles = {role: [f"fake/{role}"] for role in (*FAKE_ROLES, *cfg.roles)}
    return fake


async def open_session(
    root: Path,
    cfg: ForgeConfig,
    renderer: Renderer,
    *,
    store: Store | None = None,
    bus: EventBus | None = None,
    executor: Executor | None = None,
    headless: bool = False,
    session: Session | None = None,
) -> Ctx:
    """A Ctx for a new (or the given) session, with local defaults for every missing port."""
    root = root.resolve()
    store = store or default_store()
    session = session or await store.create_session(str(root))
    ctx = Ctx(
        session=session,
        cfg=cfg,
        root=root,
        cwd=root,
        store=store,
        bus=bus or MemoryBus(),
        executor=executor or LocalExecutor(root),
        renderer=renderer,
        ledger=ReadLedger(),
        permissions=Permissions(cfg),
        hooks=Hooks(cfg),
        headless=headless,
    )
    ctx.state.team = AgentRegistry()
    ctx.state.browser_factory = PlaywrightBrowsers(cfg.browser)  # starts on first use
    ctx.state.preview_browsers = PlaywrightBrowsers(cfg.browser, allow_local=True)
    if sys.platform == "darwin" and shutil.which("xcodebuild"):
        ctx.state.apple = XcodeBuilder(root, cfg.apple)
    if cfg.mcp_servers:
        await connect_mcp(ctx)
    await ctx.hooks.run("session_start", {"session_id": session.id, "cwd": str(root)}, ctx)
    return ctx


async def connect_mcp(ctx: Ctx) -> None:
    """Connect the configured MCP servers; a server that fails is reported and skipped."""
    hub = McpHub(ctx.cfg, ctx.root)
    for problem in await hub.connect():
        await publish_error(ctx, problem)
    ctx.state.mcp = hub


def default_store() -> SqliteStore:
    """The session database in ~/.forge (or $FORGE_HOME)."""
    return SqliteStore(forge_home() / "forge.db")


async def show_events(events: AsyncIterator[Event], renderer: Renderer) -> None:
    """Hand every event of a subscription to the renderer until the session is done."""
    async for event in events:
        await renderer.show(event)


async def close_session(ctx: Ctx) -> None:
    """Stop background jobs and shells that belong to the session; remove unkept worktrees."""
    if ctx.state.team is not None:
        await ctx.state.team.close(ctx)
    if ctx.state.mcp is not None:
        await ctx.state.mcp.close()
    await ctx.state.monitors.close()
    for browser in ctx.state.browsers.values():
        await browser.close()
    ctx.state.browsers.clear()
    for factory in (ctx.state.browser_factory, ctx.state.preview_browsers):
        if factory is not None:
            await factory.close()
    if ctx.state.apple is not None:
        await ctx.state.apple.close()
    for port in (ctx.executor, ctx.store):
        close = getattr(port, "close", None)
        if close is not None:
            await close()
