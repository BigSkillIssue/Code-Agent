"""One PostgreSQL 16 per app and environment: its own gVisor container on the app's network,
its data in a volume of its own, its password kept on the host (readable by the worker only).
Also a throwaway one for the host's checks (its data in memory), and the dump before a
migration (`<data>/apps/<app>-<environment>/backups/`, the newest few kept)."""

import asyncio
import json
import secrets
import time
from pathlib import Path

from forge_hostworker.docker import Docker, HostError

IMAGE = "postgres:16"
READY_TIMEOUT_S = 90
DUMP_TIMEOUT_S = 3600
KEEP_DUMPS = 5
# The official image starts as root to set up its folder, then runs as `postgres`.
CAPABILITIES = ("CHOWN", "DAC_OVERRIDE", "FOWNER", "SETGID", "SETUID")


def container(app: str, environment: str) -> str:
    """The database container's name."""
    return f"forge-{app}-{environment}-db"


def password(folder: Path) -> str:
    """The app's database password, made once."""
    path = folder / "db.json"
    if path.is_file():
        return str(json.loads(path.read_text(encoding="utf-8"))["password"])
    folder.mkdir(parents=True, exist_ok=True)
    chosen = secrets.token_urlsafe(24)
    path.touch(mode=0o600)
    path.write_text(json.dumps({"password": chosen}), encoding="utf-8")
    return chosen


def run_args(
    name: str, network: str, env_file: Path, memory_mb: int, app: str, data: str = ""
) -> list[str]:
    """`docker run` for the database; its data in the volume `data` (empty: its own volume)."""
    caps = [arg for cap in CAPABILITIES for arg in ("--cap-add", cap)]
    return [
        "run",
        "-d",
        "--name",
        name,
        "--runtime=runsc",
        "--cap-drop",
        "ALL",
        *caps,
        "--security-opt",
        "no-new-privileges",
        "--restart",
        "unless-stopped",
        "--network",
        network,
        "--memory",
        f"{memory_mb}m",
        "--pids-limit",
        "256",
        "--read-only",
        "--tmpfs",
        "/tmp",
        "--tmpfs",
        "/var/run/postgresql",
        *(
            ["--tmpfs", "/var/lib/postgresql/data"]
            if data == "memory"
            else ["-v", f"{data or name + '-data'}:/var/lib/postgresql/data"]
        ),
        "--env-file",
        str(env_file),
        "--label",
        f"forge.app={app}",
        "--label",
        "forge.role=database",
        IMAGE,
    ]


async def ensure_database(
    docker: Docker, app: str, environment: str, network: str, folder: Path, memory_mb: int
) -> str:
    """Start the app's database if needed and wait until it answers; its URL for the app."""
    name = container(app, environment)
    chosen = password(folder)
    state = await docker("container", "inspect", "-f", "{{.State.Running}}", name)
    if state.code != 0:
        env_file = folder / f"db-{secrets.token_hex(4)}.env"
        env_file.touch(mode=0o600)
        env_file.write_text(f"POSTGRES_USER=app\nPOSTGRES_DB=app\nPOSTGRES_PASSWORD={chosen}\n")
        try:
            started = await docker(*run_args(name, network, env_file, memory_mb, app))
        finally:
            env_file.unlink(missing_ok=True)
        if started.code != 0:
            raise HostError(
                f"the database could not start: {started.tail()[-500:]}",
                hint="see `docker logs` of the database container",
            )
    elif state.out.strip() != "true":
        await docker("start", name)
    await wait_ready(docker, name)
    return f"postgresql://app:{chosen}@{name}:5432/app"


async def wait_ready(docker: Docker, name: str, timeout_s: float = READY_TIMEOUT_S) -> None:
    """Until the database accepts connections."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_s
    while True:
        ready = await docker("exec", name, "pg_isready", "-U", "app", "-d", "app", timeout=30)
        if ready.code == 0:
            return
        if loop.time() > deadline:
            raise HostError("the database did not get ready", hint=f"see `docker logs {name}`")
        await asyncio.sleep(1)


async def throwaway_database(
    docker: Docker, name: str, network: str, app: str, folder: Path
) -> str:
    """A database for the host's checks, its data in memory (remove the container afterwards)."""
    chosen = secrets.token_urlsafe(16)
    folder.mkdir(parents=True, exist_ok=True)
    env_file = folder / f"db-{secrets.token_hex(4)}.env"
    env_file.touch(mode=0o600)
    env_file.write_text(f"POSTGRES_USER=app\nPOSTGRES_DB=app\nPOSTGRES_PASSWORD={chosen}\n")
    try:
        started = await docker(*run_args(name, network, env_file, 512, app, data="memory"))
    finally:
        env_file.unlink(missing_ok=True)
    if started.code != 0:
        raise HostError(f"the check's database could not start: {started.tail()[-500:]}",
                        hint="see the Docker daemon's log")  # fmt: skip
    await wait_ready(docker, name)
    return f"postgresql://app:{chosen}@{name}:5432/app"


async def dump_database(docker: Docker, app: str, environment: str, folder: Path, tag: str) -> Path:
    """The app's database dumped (pg_dump's own format) into its folder; the newest few kept."""
    name = container(app, environment)
    state = await docker("container", "inspect", "-f", "{{.State.Running}}", name)
    if state.code != 0:
        raise HostError("the app has no database on this host yet", hint="nothing to back up")
    if state.out.strip() != "true":
        await docker("start", name)
    await wait_ready(docker, name)
    backups = folder / "backups"
    backups.mkdir(parents=True, exist_ok=True)
    target = backups / f"{int(time.time())}-{tag}.dump"
    target.touch(mode=0o600)  # the app's users' data
    dumped = await docker("exec", name, "pg_dump", "-U", "app", "-d", "app", "-Fc",
                          timeout=DUMP_TIMEOUT_S, out=target)  # fmt: skip
    if dumped.code != 0:
        target.unlink(missing_ok=True)
        raise HostError(f"pg_dump failed: {dumped.tail()[-500:]}", hint=f"see `docker logs {name}`")
    for old in sorted(backups.glob("*.dump"))[:-KEEP_DUMPS]:
        old.unlink(missing_ok=True)
    return target
