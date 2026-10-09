"""Starting and stopping the server's parts, and how chats are configured for their sandbox."""

import asyncio
import logging
import secrets
import sys
from pathlib import Path
from typing import Any

from sqlalchemy import select, update

from forge_web.admin_api import load_saved_settings
from forge_web.apple.jobs import AppleJobs
from forge_web.apple.sandbox_api import apple_routes
from forge_web.auth.dev import ensure_dev_user
from forge_web.auth.oauth_providers import SignIn, load_providers
from forge_web.chats.runs import RunManager
from forge_web.containers.docker import DockerDriver
from forge_web.containers.driver import ContainerDriver
from forge_web.containers.local import LocalDriver
from forge_web.db.engine import Database
from forge_web.db.models import Chat, Project, User
from forge_web.db.writer import EventWriter
from forge_web.egress import Egress, EgressPolicy
from forge_web.fake import fake_script
from forge_web.gateway.proxy import Gateway, PrivateServer
from forge_web.gateway.tokens import TOKEN_ENV
from forge_web.gateway.upstreams import worker_providers
from forge_web.hub import Hub
from forge_web.preview_auth import PreviewAccess
from forge_web.quotas import enforce_disk
from forge_web.sandbox_client import ForwardTarget, SandboxClient
from forge_web.services import Services
from forge_web.settings import WebSettings
from forge_web.vault import Vault, load_master_key

log = logging.getLogger(__name__)
# Fixed ports inside a container (its loopback is its own); local sandboxes pick free ones.
GATEWAY_PORT, EGRESS_PORT = 47101, 47102


def make_driver(settings: WebSettings) -> ContainerDriver:
    """The sandbox driver the settings ask for."""
    if settings.sandbox.isolation == "local":
        return LocalDriver(settings.data_dir)
    egress = EGRESS_PORT if settings.egress.enabled else None
    return DockerDriver(settings.sandbox, egress_port=egress)


def chat_options(settings: WebSettings) -> Any:
    """A function giving the worker options of a chat (with its project's gateway port)."""
    script = fake_script(settings.dev.fake_script) if settings.dev.fake else None
    sandbox_mode = "workspace-write" if settings.sandbox.isolation == "local" else "full-access"

    def options_for(chat: Chat, link: dict[str, Any]) -> dict[str, Any]:
        options: dict[str, Any] = {"mode": chat.mode, "sandbox_mode": sandbox_mode}
        if chat.model:
            options["model"] = chat.model
        if link.get("gateway_port"):
            base = f"http://127.0.0.1:{link['gateway_port']}"
            options["providers"] = worker_providers(base, TOKEN_ENV)
            if link.get("apple") and settings.apple.enabled:
                # Builds go through the gateway to the Macs; the guidelines are checked.
                options.update(apple_url=base, apple_token_env=TOKEN_ENV, apple_review=True,
                               apple_max_mb=settings.apple.max_source_mb)  # fmt: skip
        if script is not None:
            options["fake_script"] = script
        return options

    return options_for


def link_setup(settings: WebSettings, docker: bool, db: Database) -> Any:
    """What the server sets up in a sandbox each time it connects: forwards to its targets."""

    async def on_link(project_id: str, client: SandboxClient) -> dict[str, Any]:
        port = GATEWAY_PORT if docker else 0
        gateway = await client.call("forward.listen", {"target": "gateway", "port": port})
        if docker and settings.egress.enabled:
            await client.call("forward.listen", {"target": "egress", "port": EGRESS_PORT})
        async with db.session() as session:
            kind = await session.scalar(select(Project.kind).where(Project.id == project_id))
        return {"gateway_port": int(gateway["port"]), "apple": kind == "apple"}

    return on_link


async def start_services(settings: WebSettings, driver: ContainerDriver | None) -> Services:
    """Open the database, start the writer, the gateway and the sandbox connections."""
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    db = Database(settings.database_url())
    await db.migrate()
    await load_saved_settings(db, settings)  # what admins changed in the web UI
    writer = EventWriter(db)
    writer.start()
    vault = Vault(load_master_key(settings.data_dir))
    chosen = driver or make_driver(settings)
    docker = chosen.name != "local"
    holder: dict[str, RunManager] = {}
    gateway = Gateway(db, vault, settings.gateway, lambda chat_id: holder["runs"].working(chat_id))
    apple = AppleJobs(db, settings.apple, settings.data_dir)
    await apple.start()
    gateway.routers.append(apple_routes(gateway, apple, settings.apple))
    gateway_server = PrivateServer(gateway.app(), settings.data_dir / "run")
    await gateway_server.start()
    policy = EgressPolicy(settings.egress.allow, settings.egress.allow_private)
    egress = Egress(policy)

    def targets_for(_project_id: str) -> dict[str, ForwardTarget]:
        targets: dict[str, ForwardTarget] = {"gateway": gateway_server.connect}
        if docker and settings.egress.enabled:
            targets["egress"] = egress.connect
        return targets

    hub = Hub()
    runs = RunManager(
        db, writer, chosen, hub, chat_options(settings), targets_for,
        env_for=lambda chat: {TOKEN_ENV: gateway.token_for(chat)},
        on_link=link_setup(settings, docker, db), run_seconds=settings.gateway.run_minutes * 60,
        max_log_bytes=settings.quotas.chat_log_mb * 1024 * 1024,
    )  # fmt: skip
    holder["runs"] = runs
    services = Services(
        settings=settings, db=db, writer=writer, driver=chosen, hub=hub, runs=runs, vault=vault,
        gateway=gateway, gateway_server=gateway_server, egress=egress,
        sign_in=SignIn(load_providers(settings.auth.providers)),
        previews=PreviewAccess(vault.derive("preview")), apple=apple,
    )  # fmt: skip
    await after_start(services, docker)
    return services


async def load_folders(services: Services) -> None:
    """Tell the driver which projects are server folders."""
    async with services.db.session() as session:
        rows = await session.execute(
            select(Project.id, Project.folder).where(Project.source == "folder")
        )
        for project_id, folder in rows:
            if folder:
                services.driver.folders[project_id] = Path(folder)


async def after_start(services: Services, docker: bool) -> None:
    """Resume or reset chats, start background work, set up development sign-in."""
    await load_folders(services)
    if not docker:
        # Local sandboxes end with the server, so nothing can still be running.
        async with services.db.session() as session, session.begin():
            await session.execute(update(Chat).where(Chat.state != "idle").values(state="idle"))
    else:
        services.tasks.append(asyncio.create_task(resume_active(services)))
        services.tasks.append(asyncio.create_task(reap_idle(services)))
        services.tasks.append(asyncio.create_task(watch_disk(services)))
    if services.settings.dev.enabled:
        user = await ensure_dev_user(services.db)
        services.dev_token, services.dev_user_id = secrets.token_urlsafe(24), user.id
        link = f"{services.settings.base_url()}/api/auth/dev-login?token={services.dev_token}"
        print(f"\nForge Web (development mode): open {link}\n", file=sys.stderr, flush=True)
    elif not await any_user(services):
        services.setup_token = secrets.token_urlsafe(24)
        link = f"{services.settings.base_url()}/setup#token={services.setup_token}"
        print(
            f"\nForge Web: create the first admin account at {link}\n", file=sys.stderr, flush=True
        )
    if not docker and not services.settings.dev.enabled:
        log.warning(
            "local isolation: everyone who signs in can run commands on this machine as the "
            "server's user; sign-up is limited to invites. Use Docker isolation for other people."
        )


async def any_user(services: Services) -> bool:
    """At least one account exists."""
    async with services.db.session() as session:
        return await session.scalar(select(User.id).limit(1)) is not None


async def resume_active(services: Services) -> None:
    """After a restart, follow the chats that were still working (their containers kept going)."""
    async with services.db.session() as session:
        chats = list(
            await session.scalars(select(Chat).where(Chat.state.in_(["running", "waiting"])))
        )
    for chat in chats:
        try:
            services.runs.allow_run(await services.runs.open(chat))  # it ran before the restart
        except Exception as err:
            log.warning("could not resume chat %s: %s", chat.id, err)


async def reap_idle(services: Services) -> None:
    """Stop project containers nobody used for a while."""
    while True:
        idle = services.settings.sandbox.idle_minutes * 60  # admins may change it
        await asyncio.sleep(min(60.0, idle / 2))
        try:
            await services.runs.reap(idle)
        except Exception:
            log.exception("stopping idle sandboxes failed")


async def watch_disk(services: Services) -> None:
    """Measure the projects' disk use outside their sandboxes now and then; stop those over."""
    while True:
        await asyncio.sleep(services.settings.quotas.disk_check_minutes * 60)
        try:
            await enforce_disk(services)
        except Exception:
            log.exception("checking the projects' disk use failed")


async def stop_services(services: Services) -> None:
    """Close everything in reverse order (containers keep running)."""
    for task in services.tasks:
        task.cancel()
    await services.runs.close()
    await services.egress.close()
    await services.gateway_server.stop()
    await services.gateway.close()
    await services.sign_in.close()
    await services.writer.close()
    await services.db.close()
