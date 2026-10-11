"""Metering of hosted apps (W29b): what each app and environment uses on this host, summed over
its containers (its services and its database): CPU (percent of one core), memory, network
traffic since its containers started, and disk (its volumes and its folder on the host). The
worker reports it to Forge Web every few minutes.
"""

import asyncio
import json
import re
import shutil
from pathlib import Path

from forge_hostworker import postgres
from forge_hostworker.docker import Docker
from forge_hostworker.storage import files_volume
from forge_hostworker.wire import AppUsage, HostUsage

UNITS = {"b": 1, "kb": 1e3, "mb": 1e6, "gb": 1e9, "tb": 1e12, "kib": 1024, "mib": 1024**2,
         "gib": 1024**3, "tib": 1024**4}  # fmt: skip
SIZE = re.compile(r"^\s*([0-9.]+)\s*([A-Za-z]*)\s*$")
DB_NAME = re.compile(r"^forge-(?P<app>[a-z][a-z0-9-]*)-(?P<env>staging|production)-db$")


def megabytes(text: str) -> float:
    """A size as Docker prints it ("1.2kB", "3.4MiB", "0B") in megabytes."""
    found = SIZE.match(text)
    if not found:
        return 0.0
    unit = UNITS.get((found.group(2) or "b").lower(), 1)
    return float(found.group(1)) * unit / 1e6


def owner(name: str, app: str, environment: str) -> tuple[str, str] | None:
    """Which app and environment a container belongs to (its labels, or a database's name)."""
    if app and environment:
        return app, environment
    found = DB_NAME.match(name)
    return (found["app"], found["env"]) if found else None


async def containers(docker: Docker) -> dict[str, tuple[str, str, bool]]:
    """The apps' containers: name -> (app, environment, running); checks and builds are not."""
    listed = await docker("ps", "-a", "--filter", "label=forge.app", "--format",
                          '{{.Names}}\t{{.Label "forge.app"}}\t{{.Label "forge.env"}}\t'
                          '{{.Label "forge.role"}}\t{{.State}}')  # fmt: skip
    found: dict[str, tuple[str, str, bool]] = {}
    for line in listed.out.splitlines():
        parts = (line.split("\t") + [""] * 5)[:5]
        name, app, environment, role, state = parts
        if role in ("check", "build", "migrate"):
            continue
        belongs = owner(name, app, environment)
        if belongs is not None:
            found[name] = (*belongs, state == "running")
    return found


async def usage(docker: Docker, data_dir: Path) -> HostUsage:
    """What every app uses on this host now."""
    owned = await containers(docker)
    apps: dict[tuple[str, str], AppUsage] = {}
    for app, environment, running in owned.values():
        entry = apps.setdefault((app, environment), AppUsage(app=app, environment=environment))
        entry.containers += 1
        entry.running += int(running)
    live = [name for name, (_, _, running) in owned.items() if running]
    if live:
        stats = await docker("stats", "--no-stream", "--format", "{{json .}}", *live)
        for line in stats.out.splitlines():
            add_stats(apps, owned, line)
    await add_disk(docker, data_dir, apps)
    free = shutil.disk_usage(data_dir if data_dir.exists() else data_dir.parent).free / 1e9
    return HostUsage(apps=list(apps.values()), disk_free_gb=round(free, 2))


def add_stats(apps: dict[tuple[str, str], AppUsage], owned: dict[str, tuple[str, str, bool]],
              line: str) -> None:  # fmt: skip
    """Add one container's `docker stats` line to its app."""
    try:
        row = json.loads(line)
    except ValueError:
        return
    belongs = owned.get(str(row.get("Name", "")))
    if belongs is None:
        return
    entry = apps[(belongs[0], belongs[1])]
    entry.cpu_percent += float(str(row.get("CPUPerc", "0")).rstrip("%") or 0)
    entry.memory_mb += megabytes(str(row.get("MemUsage", "0B")).split("/")[0])
    received, _, sent = str(row.get("NetIO", "0B / 0B")).partition("/")
    entry.net_rx_mb += megabytes(received)
    entry.net_tx_mb += megabytes(sent)


async def add_disk(docker: Docker, data_dir: Path, apps: dict[tuple[str, str], AppUsage]) -> None:
    """Each app's volumes (database, files) and its folder on the host."""
    listed = await docker("system", "df", "-v", "--format", "{{json .Volumes}}", timeout=300)
    try:
        volumes = json.loads(listed.out or "[]") or []
    except ValueError:
        volumes = []
    sizes = {str(v.get("Name", "")): megabytes(str(v.get("Size", "0B"))) for v in volumes}
    for (app, environment), entry in apps.items():
        own = (files_volume(app, environment), postgres.container(app, environment) + "-data")
        entry.disk_mb += sum(sizes.get(name, 0.0) for name in own)
        folder = data_dir / "apps" / f"{app}-{environment}"
        entry.disk_mb += await asyncio.to_thread(folder_mb, folder)


def folder_mb(folder: Path) -> float:
    """The size of a folder's files, in megabytes."""
    if not folder.is_dir():
        return 0.0
    return sum(path.stat().st_size for path in folder.rglob("*") if path.is_file()) / 1e6
