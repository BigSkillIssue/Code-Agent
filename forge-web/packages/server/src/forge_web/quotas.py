"""Quotas: how many projects a user may own and how much disk a project may use.

Disk use is measured two ways: by the sandbox (quick, for uploads and messages) and by the
container engine outside it (trusted, every few minutes). Checks use the larger number, and a
project whose measured volumes are over the quota has its container stopped: the sandbox could
lie about its own use, and programs in it write without asking the server.
"""

import logging
import time

from fastapi import HTTPException
from sqlalchemy import func, select

from forge_web.db.models import Project, User
from forge_web.sandbox_calls import result_dict, sandbox_call
from forge_web.services import Services

log = logging.getLogger(__name__)
MB = 1024 * 1024
USAGE_SECONDS = 60.0  # how long a measured disk use is trusted


async def check_project_count(services: Services, user: User) -> None:
    """403 if the user may not create another project."""
    limit = services.settings.quotas.projects_per_user
    if not limit or user.role == "admin":
        return
    async with services.db.session() as session:
        owned = await session.scalar(
            select(func.count()).select_from(Project).where(Project.owner_id == user.id)
        )
    if int(owned or 0) >= limit:
        raise HTTPException(403, f"you have reached the limit of {limit} projects")


def disk_limit(services: Services) -> int | None:
    """Bytes a project may use, or None for no limit."""
    mb = services.settings.quotas.project_disk_mb
    return mb * MB if mb else None


async def disk_use(services: Services, project_id: str, *, fresh: bool = False) -> int:
    """Bytes the project's files use (measured at most once a minute unless `fresh`)."""
    cached = services.disk_use.get(project_id)
    if cached and not fresh and time.monotonic() - cached[0] < USAGE_SECONDS:
        return cached[1]
    found = result_dict(await sandbox_call(services, project_id, "fs.usage"))
    used = found.get("bytes")
    size = used if isinstance(used, int) and used >= 0 else 0
    size = max(size, services.host_disk_use.get(project_id, 0))
    services.disk_use[project_id] = (time.monotonic(), size)
    return size


async def enforce_disk(services: Services) -> list[str]:
    """Measure every project outside its sandbox; stop the containers of projects over quota."""
    services.host_disk_use = await services.driver.disk_use()
    limit = disk_limit(services)
    over = [p for p, used in services.host_disk_use.items() if limit is not None and used > limit]
    for project_id in over:
        log.warning("project %s uses more disk than its quota: stopping it", project_id)
        services.disk_use.pop(project_id, None)
        await services.runs.forget_project(project_id)
        await services.driver.stop(project_id)
    return over


async def disk_room(services: Services, project_id: str) -> int | None:
    """Bytes the project may still add, or None for no limit."""
    limit = disk_limit(services)
    if limit is None:
        return None
    return max(0, limit - await disk_use(services, project_id, fresh=True))


async def check_disk(services: Services, project_id: str) -> None:
    """409 if the project uses more than its quota."""
    limit = disk_limit(services)
    if limit is None:
        return
    used = await disk_use(services, project_id)
    if used > limit:
        raise HTTPException(
            409, f"this project uses {used // MB} MB of its {limit // MB} MB; delete files first"
        )
