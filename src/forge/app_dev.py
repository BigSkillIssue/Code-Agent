"""`forge app dev` (S65): the product running locally as hosting would run it, on a throwaway
PostgreSQL, with every service's output in one stream.

Python services are installed from their lockfile, migrated and started with their manifest
command; web clients run their dev server (`npm run dev`) with `/api` pointing at the server.
Everything runs as background jobs through the Executor port and stops together.
"""

import asyncio
import contextlib
import shutil
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from forge.app_manifest import AppManifest, Service, load_manifest
from forge.ports import Command, Executor, JobNotFoundError, SandboxPolicy
from forge.runtime.postgres import Postgres, PostgresError, start_postgres, stop_postgres

POLL_S = 0.3
INSTALL_TIMEOUT_S = 900
STORAGE = Path(".forge") / "dev" / "storage"

Show = Callable[[str], None]


@dataclass
class Running:
    """A started service and how much of its output was shown."""

    name: str
    job_id: str
    line: int = 0


def api_url(manifest: AppManifest) -> str:
    """Where the web client's dev server sends /api: the service routed there."""
    api = next((s for s in manifest.services if s.route == "/api"), None)
    api = api or next((s for s in manifest.services if s.runtime == "python3.12"), None)
    return f"http://localhost:{api.port}" if api else ""


def app_url(manifest: AppManifest) -> str:
    """Where people open the product: the service routed at /."""
    web = next((s for s in manifest.services if s.route == "/"), None)
    return f"http://localhost:{web.port}" if web else api_url(manifest)


async def run_dev(
    root: Path,
    executor: Executor,
    policy: SandboxPolicy,
    show: Show,
    stop: asyncio.Event,
    database_url: str | None = None,
) -> int:
    """Run the product until `stop` is set (0) or a service ends (1)."""
    manifest = load_manifest(root)
    if isinstance(manifest, list):
        for problem in manifest:
            show(f"forge.app.toml: {problem.field or 'file'}: {problem.message}")
        return 1
    pg: Postgres | None = None
    running: list[Running] = []
    try:
        if manifest.database is not None and database_url is None:
            pg = await start_postgres(executor, policy)
            database_url = pg.url
            show(f"database: a throwaway PostgreSQL ({database_url})")
        for service in manifest.services:
            job = await start_service(executor, policy, root, manifest, service, database_url, show)
            if job is None:
                return 1
            running.append(Running(service.name, job))
        show(f"open {app_url(manifest)} (ctrl+c stops everything)")
        return await follow(executor, running, show, stop)
    except PostgresError as error:
        show(f"error: {error}\nhint: {error.hint}")
        return 1
    finally:
        await stop_all(executor, policy, running, pg)


async def start_service(
    executor: Executor,
    policy: SandboxPolicy,
    root: Path,
    manifest: AppManifest,
    service: Service,
    database_url: str | None,
    show: Show,
) -> str | None:
    """Prepare and start one service in the background; its job id, or None on failure."""
    folder = root / service.root
    env = {"PORT": str(service.port), "APP_URL": app_url(manifest)}
    if service.runtime == "python3.12":
        env["STORAGE_DIR"] = str(root / STORAGE / service.name)
        env |= {"DATABASE_URL": database_url} if database_url else {}
        steps = [["uv", "sync", "--locked"]]
        if database_url and (folder / "alembic.ini").is_file():
            steps.append(["uv", "run", "--locked", "alembic", "upgrade", "head"])
        argv = ["uv", "run", "--locked", *service.command]
    else:
        env["API_URL"] = api_url(manifest)
        steps = [] if (folder / "node_modules").is_dir() else [["npm", "ci", "--no-audit"]]
        argv = dev_command(service)
    for step in steps:
        if not await prepare(executor, policy, folder, step, env, service.name, show):
            return None
    cmd = Command(argv=program(argv), cwd=str(folder), env=env, timeout_s=24 * 3600)
    result = await executor.run(cmd, policy, background=True)
    if result.job_id is None:
        show(f"[{service.name}] could not start: {(result.stderr or result.stdout).strip()}")
        return None
    show(f"[{service.name}] {' '.join(argv)} (port {service.port})")
    return result.job_id


def program(argv: list[str]) -> list[str]:
    """The argv with its program's full path (npm is npm.cmd on Windows)."""
    return [shutil.which(argv[0]) or argv[0], *argv[1:]]


def dev_command(service: Service) -> list[str]:
    """How a web or Node service runs during development."""
    if service.runtime == "static":
        return ["npm", "run", "dev", "--", "--port", str(service.port), "--strictPort"]
    return list(service.command)


async def prepare(
    executor: Executor,
    policy: SandboxPolicy,
    folder: Path,
    argv: list[str],
    env: dict[str, str],
    name: str,
    show: Show,
) -> bool:
    """Run one preparation step (install, migrate); show its output when it fails."""
    show(f"[{name}] {' '.join(argv)}")
    cmd = Command(argv=program(argv), cwd=str(folder), env=env, timeout_s=INSTALL_TIMEOUT_S)
    result = await executor.run(cmd, policy)
    if result.exit_code == 0 and not result.timed_out:
        return True
    output = "\n".join(t for t in (result.stdout.strip(), result.stderr.strip()) if t)
    show(f"[{name}] failed:\n{output[-3000:]}")
    return False


async def follow(
    executor: Executor, running: list[Running], show: Show, stop: asyncio.Event
) -> int:
    """Show new output of every service until `stop` is set or a service ends."""
    while not stop.is_set():
        for service in running:
            result = await executor.job_output(service.job_id, since_line=service.line)
            for line in result.stdout.splitlines():
                show(f"[{service.name}] {line}")
            service.line = result.total_lines if result.total_lines is not None else service.line
            if result.exit_code is not None:
                show(f"[{service.name}] stopped (exit {result.exit_code}); stopping everything")
                return 1
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), POLL_S)
    return 0


async def stop_all(
    executor: Executor, policy: SandboxPolicy, running: list[Running], pg: Postgres | None
) -> None:
    """Stop every service, then the database."""
    for service in running:
        with contextlib.suppress(JobNotFoundError):
            await executor.job_stop(service.job_id)
    if pg is not None:
        await stop_postgres(executor, policy, pg)
