"""The routes of snapshots (W66).

- `/api/projects/{id}/snapshots`: the project's snapshots (owners and editors; viewers 403,
  others 404) and `.../{sid}/restore`, which brings one back as a new branch `restore/<date>`.
- `/api/host/blob/{id}`: a storage node fetches a blob being handed to it (GET) or sends one
  back that Forge Web asked for (PUT), with its host token.
"""

import re
import tempfile
import time
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse
from sqlalchemy import select

from forge_hostworker.wire import BackupError
from forge_web.audit import audit
from forge_web.auth.sessions import CurrentUser, client_ip
from forge_web.db.hosting_models import HostServer
from forge_web.db.models import Chat, User
from forge_web.db.snapshot_models import ProjectSnapshot
from forge_web.files_api import code_allowed, receive
from forge_web.gitsync import project_lock
from forge_web.hosting.deploy_api import Host
from forge_web.sandbox_calls import result_dict, sandbox_call
from forge_web.services import Services, services_of
from forge_web.snapshots.store import SnapshotProblem

SHOWN = 200
TRANSFER = ".git/forge-transfer"
BRANCH = re.compile(r"^restore/[0-9]{4}-[0-9]{2}-[0-9]{2}-[0-9]{4}(-[0-9]{1,4})?$")


def restore_branch(when: float) -> str:
    """The branch a snapshot taken at `when` comes back as: `restore/YYYY-MM-DD-HHMM` (UTC)."""
    return "restore/" + time.strftime("%Y-%m-%d-%H%M", time.gmtime(when))


def snapshots_router() -> APIRouter:
    """A project's snapshots."""
    router = APIRouter(prefix="/api/projects/{project_id}/snapshots")

    @router.get("")
    async def listed(project_id: str, request: Request, user: CurrentUser) -> dict[str, Any]:
        services = await code_allowed(request, user, project_id, "editor")
        return await overview(services, project_id)

    @router.post("/{snapshot_id}/restore")
    async def restored(
        project_id: str, snapshot_id: str, request: Request, user: CurrentUser
    ) -> dict[str, Any]:
        services = await code_allowed(request, user, project_id, "editor")
        return await restore(services, project_id, snapshot_id, user, client_ip(request))

    return router


async def overview(services: Services, project_id: str) -> dict[str, Any]:
    """The project's snapshots, newest first, and where new ones are kept."""
    async with services.db.session() as session:
        rows = list(await session.scalars(
            select(ProjectSnapshot).where(ProjectSnapshot.project_id == project_id,
                                          ProjectSnapshot.state != "deleted")
            .order_by(ProjectSnapshot.created_at.desc()).limit(SHOWN)))  # fmt: skip
        chats = {row.id: row.title for row in await session.execute(
            select(Chat.id, Chat.title).where(Chat.project_id == project_id))}  # fmt: skip
        nodes = {row.id: row.name for row in await session.execute(
            select(HostServer.id, HostServer.name))}  # fmt: skip
    node = await services.snapshots.store.node()
    return {
        "enabled": services.settings.snapshots.enabled,
        "storage": "node" if node is not None else "local",
        "snapshots": [{
            "id": row.id, "created_at": row.created_at, "chat_id": row.chat_id,
            "chat": chats.get(row.chat_id, ""), "commit": row.commit[:7], "kind": row.kind,
            "size": row.size, "state": row.state, "error": row.error,
            "where": "node" if row.node_id else "local",
            "node": nodes.get(row.node_id or "", ""),
        } for row in rows],
    }  # fmt: skip


async def restore(
    services: Services, project_id: str, snapshot_id: str, user: User, ip: str
) -> dict[str, Any]:
    """Bring a snapshot back as a new branch (the user's branch and files stay as they are)."""
    store = services.snapshots.store
    limit = services.settings.snapshots.max_mb * 1024 * 1024
    try:
        chain = await store.chain(project_id, snapshot_id)
        names = []
        async with project_lock(services, project_id):
            with tempfile.TemporaryDirectory(prefix="forge-restore-") as work:
                for row in chain:
                    sealed = await store.sealed(row, Path(work))
                    name = f"snap-{row.id}.bundle"
                    await receive(services, project_id, f"{TRANSFER}/{name}",
                                  store.plain_parts(project_id, sealed), limit)  # fmt: skip
                    names.append(name)
            params = {"bundles": names, "commit": chain[-1].commit,
                      "branch": restore_branch(chain[-1].created_at)}  # fmt: skip
            made = result_dict(await sandbox_call(services, project_id,
                                                  "git.snapshot_restore", params))  # fmt: skip
    except SnapshotProblem as problem:
        raise HTTPException(409, str(problem)) from None
    except BackupError:
        raise HTTPException(409, "the snapshot does not open with the project's key") from None
    branch = made.get("branch")
    if not isinstance(branch, str) or not BRANCH.match(branch):
        raise HTTPException(502, "the sandbox sent an unexpected answer")
    services.snapshots.mark_restored(project_id)
    await audit(services.db, "snapshot_restore", user_id=user.id, target=project_id, ip=ip,
                snapshot=snapshot_id, branch=branch)  # fmt: skip
    return {"branch": branch, "commit": chain[-1].commit}


def blob_router() -> APIRouter:
    """Storage nodes fetching and returning blobs."""
    router = APIRouter(prefix="/api/host/blob")

    @router.get("/{blob_id}")
    async def handed(blob_id: str, host: Host, request: Request) -> FileResponse:
        try:
            path = await services_of(request).snapshots.store.handed(host.id, blob_id)
        except LookupError as refusal:
            raise HTTPException(409, str(refusal)) from None
        return FileResponse(path, media_type="application/octet-stream")

    @router.put("/{blob_id}")
    async def returned(blob_id: str, host: Host, request: Request) -> dict[str, int]:
        store = services_of(request).snapshots.store
        try:
            size = await store.receive(host.id, blob_id, request.stream())
        except LookupError as refusal:
            raise HTTPException(409, str(refusal)) from None
        except ValueError as problem:
            raise HTTPException(422, str(problem)) from None
        return {"size": size}

    return router
