"""Wire the local port implementations into a Ctx for one session (used by cli, tui and api)."""

from collections.abc import AsyncIterator
from pathlib import Path

from forge.config import ForgeConfig, forge_home
from forge.ctx import Ctx
from forge.events import Event
from forge.hooks import Hooks
from forge.local.local_executor import LocalExecutor
from forge.local.memory_bus import MemoryBus
from forge.local.sqlite_store import SqliteStore
from forge.ports import EventBus, Executor, Renderer, Session, Store
from forge.providers.fake import FakeProvider
from forge.providers.registry import register_provider
from forge.runtime.ledger import ReadLedger
from forge.runtime.permissions import Permissions

FAKE_FIXTURE = Path("tests") / "fixtures" / "fake" / "hello.json"
FAKE_ROLES = (
    "refiner",
    "planner",
    "coder",
    "reviewer",
    "compressor",
    "explore",
    "tester",
    "researcher",
    "lead",
)


def use_fake_provider(cfg: ForgeConfig, script: Path | None, project_root: Path) -> FakeProvider:
    """Send every role to a FakeProvider replaying `script` (default: the hello fixture)."""
    path = script or project_root / FAKE_FIXTURE
    if path.is_file():
        fake = FakeProvider.from_file(path)
    else:
        fake = FakeProvider.from_data([{"text": "Hello from the fake provider."}])
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
    return Ctx(
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


def default_store() -> SqliteStore:
    """The session database in ~/.forge (or $FORGE_HOME)."""
    return SqliteStore(forge_home() / "forge.db")


async def show_events(events: AsyncIterator[Event], renderer: Renderer) -> None:
    """Hand every event of a subscription to the renderer until the session is done."""
    async for event in events:
        await renderer.show(event)


async def close_session(ctx: Ctx) -> None:
    """Stop background jobs and shells that belong to the session."""
    for port in (ctx.executor, ctx.store):
        close = getattr(port, "close", None)
        if close is not None:
            await close()
