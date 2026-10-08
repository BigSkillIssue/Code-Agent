"""A project's files over HTTP. For now the search behind @-mentions; the file panel follows."""

from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request

from forge_sandbox.mux import ChannelClosed
from forge_sandbox.rpc import RpcError
from forge_web.access import require_project
from forge_web.auth.sessions import CurrentUser
from forge_web.chats.api import sandbox_failure
from forge_web.containers.driver import SandboxError
from forge_web.services import services_of


def files_router() -> APIRouter:
    """`/api/projects/{id}/files/...`."""
    router = APIRouter(prefix="/api/projects/{project_id}/files")

    @router.get("/search")
    async def search(
        project_id: str,
        request: Request,
        user: CurrentUser,
        query: str = Query("", max_length=200),
        limit: int = Query(50, ge=1, le=200),
    ) -> dict[str, Any]:
        services = services_of(request)
        async with services.db.session() as session:
            await require_project(session, user, project_id)
        try:
            found = await services.runs.call(
                project_id, "git.files", {"query": query, "limit": limit}
            )
        except (RpcError, ChannelClosed, SandboxError, OSError, TimeoutError) as err:
            raise sandbox_failure(err) from None
        if not isinstance(found, dict) or not isinstance(found.get("files"), list):
            raise HTTPException(502, "the sandbox sent an unexpected answer")
        files = [f for f in found["files"] if isinstance(f, str)][:limit]
        total = found.get("total")
        return {"files": files, "total": total if isinstance(total, int) else len(files)}

    return router
