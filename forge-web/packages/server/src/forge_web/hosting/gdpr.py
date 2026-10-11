"""Export and full deletion of a hosted app (W29c), for the project's owner.

The export is a fresh backup of one environment (`ops.Backups`), which the owner downloads
decrypted: a tar with `db.dump` (`pg_dump -Fc`, read back with `pg_restore`) and `files.tar`.
Nothing readable is written on this server: the backup is decrypted while it is sent. Deleting
the app is `removal.Removals`.
"""

import asyncio
import base64
import itertools
import time
from collections.abc import Iterator
from typing import Literal

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select

from forge_hostworker.wire import BackupError, decrypt_chunks
from forge_web.audit import audit
from forge_web.auth.sessions import CurrentUser, client_ip
from forge_web.db.hosting_models import AppBackup, AppBackupKey, Deploy, HostedApp
from forge_web.files_api import allowed
from forge_web.hosting.ops import said
from forge_web.services import Services


class ExportIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    environment: Literal["staging", "production"]


async def export_of(
    services: Services, project_id: str, backup_id: str
) -> tuple[str, Iterator[bytes]]:
    """A kept backup's file name and its contents, decrypted while they are read."""
    async with services.db.session() as session:
        row = await session.get(AppBackup, backup_id)
        hosted = await session.get(HostedApp, project_id)
        key = await session.get(AppBackupKey, project_id)
    if row is None or row.project_id != project_id or hosted is None or key is None:
        raise LookupError("no such backup")
    chunks = decrypt_chunks(base64.b64decode(services.vault.decrypt(key.key)),
                            services.backups.path(project_id, row.environment, row.id))  # fmt: skip
    first = await asyncio.to_thread(next, chunks, b"")  # a backup that does not open fails here
    day = time.strftime("%Y-%m-%d", time.gmtime(row.created_at))
    return f"{hosted.app}-{row.environment}-{day}.tar", itertools.chain([first], chunks)


async def exportable(services: Services, project_id: str, environment: str) -> None:
    """409 unless the environment has a live release (suspended apps can be exported too)."""
    async with services.db.session() as session:
        hosted = await session.get(HostedApp, project_id)
        live = await session.scalar(select(Deploy.id).where(
            Deploy.project_id == project_id, Deploy.environment == environment,
            Deploy.status == "live"))  # fmt: skip
    if hosted is None or live is None:
        raise HTTPException(409, f"nothing is live in {environment}")
    if hosted.deleting_at:
        raise HTTPException(409, "the app is being deleted")


def gdpr_router() -> APIRouter:
    """`/api/projects/{id}/hosting`: export the app's data, delete the app (the owner only)."""
    router = APIRouter(prefix="/api/projects/{project_id}/hosting")

    @router.post("/export")
    async def export(
        project_id: str, body: ExportIn, request: Request, user: CurrentUser
    ) -> dict[str, bool]:
        services = await allowed(request, user, project_id, "owner")
        await exportable(services, project_id, body.environment)
        services.background(said(services.backups.backup(project_id, body.environment)))
        await audit(services.db, "hosting.export_started", user_id=user.id, target=project_id,
                    ip=client_ip(request), environment=body.environment)  # fmt: skip
        return {"started": True}

    @router.get("/backups/{backup_id}")
    async def download(
        project_id: str, backup_id: str, request: Request, user: CurrentUser
    ) -> StreamingResponse:
        services = await allowed(request, user, project_id, "owner")
        try:
            name, chunks = await export_of(services, project_id, backup_id)
        except LookupError:
            raise HTTPException(404, "no such backup") from None
        except BackupError:
            raise HTTPException(409, "the backup does not open") from None
        await audit(services.db, "hosting.export_downloaded", user_id=user.id,
                    target=project_id, ip=client_ip(request), backup=backup_id)  # fmt: skip
        disposition = f'attachment; filename="{name}"'
        return StreamingResponse(chunks, media_type="application/x-tar",
                                 headers={"Content-Disposition": disposition})  # fmt: skip

    @router.delete("")
    async def remove(project_id: str, request: Request, user: CurrentUser) -> dict[str, bool]:
        services = await allowed(request, user, project_id, "owner")
        await start_deleting(services, project_id, user.id, client_ip(request))
        return {"deleting": True}

    return router


async def start_deleting(services: Services, project_id: str, user_id: str, ip: str) -> None:
    """Begin deleting a hosted app (404 if it is not hosted, 409 while a deploy runs)."""
    try:
        await services.removals.request(project_id)
    except LookupError as error:
        raise HTTPException(404, str(error)) from None
    except ValueError as error:
        raise HTTPException(409, str(error)) from None
    await audit(services.db, "hosting.app_deletion_started", user_id=user_id, target=project_id,
                ip=ip)  # fmt: skip
