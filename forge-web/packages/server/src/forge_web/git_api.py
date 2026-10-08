"""A project's git repository over HTTP: status, diffs, staging, commits, branches, history and
the remote, push and pull. Reading needs the viewer role, changing the editor role. Pushing and
pulling (which need the network and the user's token) run in a git job outside the project."""

import re
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field

from forge_web.auth.sessions import CurrentUser, client_ip
from forge_web.db.models import User
from forge_web.files_api import MAX_PATH, allowed
from forge_web.gitsync import (
    check_branch,
    current_branch,
    local_remotes,
    pull,
    push,
    remote_problem,
)
from forge_web.sandbox_calls import result_dict, sandbox_call

CONTROL = re.compile(r"[\x00-\x1f\x7f]")


class PathsIn(BaseModel):
    """Paths to stage, unstage or discard."""

    paths: list[str] = Field(min_length=1, max_length=1000)


class CommitIn(BaseModel):
    """A commit message."""

    message: str = Field(min_length=1, max_length=10_000)


class SwitchIn(BaseModel):
    """A branch to switch to (or create)."""

    branch: str = Field(min_length=1, max_length=200)
    create: bool = False


class RemoteIn(BaseModel):
    """Where `origin` is."""

    url: str = Field(min_length=1, max_length=2000)


class SyncIn(BaseModel):
    """Push or pull: the project's branch (default: the checked-out one) and the remote's."""

    branch: str = Field(default="", max_length=200)
    remote_branch: str = Field(default="", max_length=200)


def committer(user: User) -> tuple[str, str]:
    """The name and email a commit by this user carries."""
    name = CONTROL.sub("", user.name or "").strip() or "Forge Web user"
    email = CONTROL.sub("", user.email or "").strip() or f"{user.id}@users.forge-web.invalid"
    return name, email


def git_router() -> APIRouter:
    """`/api/projects/{id}/git/...`."""
    router = APIRouter(prefix="/api/projects/{project_id}/git")
    reading_routes(router)
    changing_routes(router)
    sync_routes(router)
    return router


def reading_routes(router: APIRouter) -> None:
    """Status, diff, branches, history, remote."""

    @router.get("/status")
    async def status(project_id: str, request: Request, user: CurrentUser) -> dict[str, Any]:
        services = await allowed(request, user, project_id)
        return result_dict(await sandbox_call(services, project_id, "git.status"))

    @router.get("/diff")
    async def diff(
        project_id: str,
        request: Request,
        user: CurrentUser,
        path: str = Query("", max_length=MAX_PATH),
        staged: bool = False,
    ) -> dict[str, Any]:
        services = await allowed(request, user, project_id)
        params = {"path": path or None, "staged": staged}
        return result_dict(await sandbox_call(services, project_id, "git.diff", params))

    @router.get("/branches")
    async def branches(project_id: str, request: Request, user: CurrentUser) -> dict[str, Any]:
        services = await allowed(request, user, project_id)
        return result_dict(await sandbox_call(services, project_id, "git.branches"))

    @router.get("/log")
    async def log(
        project_id: str, request: Request, user: CurrentUser, limit: int = Query(50, ge=1, le=500)
    ) -> dict[str, Any]:
        services = await allowed(request, user, project_id)
        return result_dict(await sandbox_call(services, project_id, "git.log", {"limit": limit}))

    @router.get("/remote")
    async def remote(project_id: str, request: Request, user: CurrentUser) -> dict[str, Any]:
        services = await allowed(request, user, project_id)
        found = result_dict(await sandbox_call(services, project_id, "git.remote"))
        url = found.get("url") if isinstance(found.get("url"), str) else None
        return {"url": url, "problem": remote_problem(url) if url else None}


def changing_routes(router: APIRouter) -> None:
    """Stage, unstage, discard, commit, switch branches, set the remote."""

    for action in ("stage", "unstage", "discard"):
        add_paths_route(router, action)

    @router.post("/commit")
    async def commit(
        project_id: str, body: CommitIn, request: Request, user: CurrentUser
    ) -> dict[str, Any]:
        services = await allowed(request, user, project_id, "editor")
        name, email = committer(user)
        params = {"message": body.message, "name": name, "email": email}
        return result_dict(await sandbox_call(services, project_id, "git.commit", params))

    @router.post("/switch")
    async def switch(
        project_id: str, body: SwitchIn, request: Request, user: CurrentUser
    ) -> dict[str, Any]:
        services = await allowed(request, user, project_id, "editor")
        params = {"branch": body.branch, "create": body.create}
        return result_dict(await sandbox_call(services, project_id, "git.switch", params))

    @router.put("/remote")
    async def set_remote(
        project_id: str, body: RemoteIn, request: Request, user: CurrentUser
    ) -> dict[str, Any]:
        services = await allowed(request, user, project_id, "editor")
        local = local_remotes(services) and body.url.startswith("file://")
        problem = None if local else remote_problem(body.url)
        if problem:
            raise HTTPException(422, problem)
        params = {"url": body.url.strip()}
        return result_dict(await sandbox_call(services, project_id, "git.set_remote", params))


def sync_routes(router: APIRouter) -> None:
    """Push to and pull from the remote (through a git job outside the project)."""

    @router.post("/push")
    async def push_route(
        project_id: str, body: SyncIn, request: Request, user: CurrentUser
    ) -> dict[str, Any]:
        services = await allowed(request, user, project_id, "editor")
        branch = check_branch(body.branch or await current_branch(services, project_id))
        remote_branch = check_branch(body.remote_branch or branch)
        return await push(services, project_id, user, branch, remote_branch, client_ip(request))

    @router.post("/pull")
    async def pull_route(
        project_id: str, body: SyncIn, request: Request, user: CurrentUser
    ) -> dict[str, Any]:
        services = await allowed(request, user, project_id, "editor")
        branch = check_branch(body.branch or await current_branch(services, project_id))
        remote_branch = check_branch(body.remote_branch or branch)
        return await pull(services, project_id, user, branch, remote_branch, client_ip(request))


def add_paths_route(router: APIRouter, action: str) -> None:
    """POST /git/<action> {paths}."""

    async def route(
        project_id: str, body: PathsIn, request: Request, user: CurrentUser
    ) -> dict[str, Any]:
        services = await allowed(request, user, project_id, "editor")
        return result_dict(
            await sandbox_call(services, project_id, f"git.{action}", {"paths": body.paths})
        )

    router.add_api_route(f"/{action}", route, methods=["POST"], name=f"git_{action}")
