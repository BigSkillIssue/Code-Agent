"""A throwaway PostgreSQL for running and checking a product (S65): a fresh cluster in a temp
folder, reachable only through a Unix socket (on Windows: localhost on a free port), removed when
it stops. Commands run through the Executor port, like every other command Forge runs.
"""

import glob
import os
import shutil
import socket
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

from forge.ports import Command, CommandResult, Executor, SandboxPolicy

USER = "forge"
DATABASE = "app"
START_TIMEOUT_S = 60
# Where PostgreSQL's programs live when they are not on PATH (Debian/Ubuntu, Homebrew, Windows).
SEARCH = (
    "/usr/lib/postgresql/*/bin",
    "/usr/pgsql-*/bin",
    "/opt/homebrew/opt/postgresql@*/bin",
    "/usr/local/opt/postgresql@*/bin",
    "/Applications/Postgres.app/Contents/Versions/*/bin",
    "C:/Program Files/PostgreSQL/*/bin",
)


class PostgresError(Exception):
    """PostgreSQL could not be started or used; `hint` says what to do."""

    def __init__(self, message: str, hint: str) -> None:
        super().__init__(message)
        self.hint = hint


@dataclass
class Postgres:
    """A running throwaway cluster."""

    bin_dir: Path
    data_dir: Path
    host: str  # the socket folder, or 127.0.0.1
    port: int

    @property
    def url(self) -> str:
        """The URL of the cluster's `app` database."""
        if self.host.startswith("/"):
            return f"postgresql://{USER}@/{DATABASE}?host={self.host}&port={self.port}"
        return f"postgresql://{USER}@{self.host}:{self.port}/{DATABASE}"


def find_bin_dir() -> Path | None:
    """The folder holding initdb and pg_ctl, preferring PATH, then the newest known install."""
    found = shutil.which("initdb")
    if found:
        return Path(found).parent
    for pattern in SEARCH:
        folders = sorted(glob.glob(pattern), key=_version_key, reverse=True)
        for folder in folders:
            if (Path(folder) / _exe("initdb")).exists():
                return Path(folder)
    return None


def _version_key(folder: str) -> tuple[int, ...]:
    """Sort key: the version numbers in a folder name, so 16 comes before 9."""
    digits = "".join(c if c.isdigit() else " " for c in folder).split()
    return tuple(int(d) for d in digits)


def _exe(name: str) -> str:
    """A program's file name on this OS."""
    return f"{name}.exe" if sys.platform == "win32" else name


def free_port() -> int:
    """A TCP port nobody listens on right now."""
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


async def start_postgres(executor: Executor, policy: SandboxPolicy) -> Postgres:
    """Make and start a fresh cluster with an empty `app` database."""
    bin_dir = find_bin_dir()
    if bin_dir is None:
        raise PostgresError(
            "PostgreSQL is not installed (no initdb found)",
            hint="install PostgreSQL 16 (apt install postgresql, brew install postgresql@16), "
            "or pass --database-url with a throwaway database",
        )
    if sys.platform != "win32" and os.geteuid() == 0:
        raise PostgresError(
            "PostgreSQL refuses to run as root",
            hint="run Forge as a normal user, or pass --database-url with a throwaway database",
        )
    folder = Path(tempfile.mkdtemp(prefix="forge-pg-"))
    unix = sys.platform != "win32"
    pg = Postgres(bin_dir, folder / "data", str(folder) if unix else "127.0.0.1", 5432)
    if not unix:
        pg.port = free_port()
    try:
        await _start(executor, policy, pg)
    except PostgresError:
        shutil.rmtree(folder, ignore_errors=True)
        raise
    return pg


async def _start(executor: Executor, policy: SandboxPolicy, pg: Postgres) -> None:
    """initdb, pg_ctl start and createdb; each failure says which step and why."""
    initdb = [str(pg.bin_dir / _exe("initdb")), "-D", str(pg.data_dir), "-U", USER]
    await _run(executor, policy, pg, [*initdb, "--auth=trust", "-E", "UTF8", "--no-sync"], "initdb")
    if pg.host.startswith("/"):  # pg_ctl passes these through a shell, so '' stays empty
        options = f"-k {pg.host} -c listen_addresses='' -p {pg.port} -c fsync=off"
    else:
        options = f"-c listen_addresses={pg.host} -p {pg.port} -c fsync=off"
    log = str(pg.data_dir.parent / "postgres.log")
    pg_ctl = [str(pg.bin_dir / _exe("pg_ctl")), "-D", str(pg.data_dir), "-l", log]
    await _run(executor, policy, pg, [*pg_ctl, "-o", options, "-w", "start"], "pg_ctl start")
    createdb = [str(pg.bin_dir / _exe("createdb")), "-h", pg.host, "-p", str(pg.port)]
    await _run(executor, policy, pg, [*createdb, "-U", USER, DATABASE], "createdb")


async def _run(
    executor: Executor, policy: SandboxPolicy, pg: Postgres, argv: list[str], step: str
) -> CommandResult:
    """Run one PostgreSQL program; a failure raises with its output."""
    cmd = Command(argv=argv, cwd=str(pg.data_dir.parent), timeout_s=START_TIMEOUT_S)
    result = await executor.run(cmd, policy)
    if result.exit_code != 0:
        output = (result.stderr or result.stdout).strip()[-1500:]
        hint = "fix what it says, or pass --database-url with a throwaway database"
        raise PostgresError(f"{step} failed: {output}", hint=hint)
    return result


async def stop_postgres(executor: Executor, policy: SandboxPolicy, pg: Postgres) -> None:
    """Stop the cluster and remove its folder; nothing is kept."""
    pg_ctl = [str(pg.bin_dir / _exe("pg_ctl")), "-D", str(pg.data_dir), "-m", "immediate", "stop"]
    cmd = Command(argv=pg_ctl, cwd=str(pg.data_dir.parent), timeout_s=START_TIMEOUT_S)
    try:
        await executor.run(cmd, policy)
    finally:
        shutil.rmtree(pg.data_dir.parent, ignore_errors=True)
