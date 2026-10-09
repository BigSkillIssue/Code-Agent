"""Projects: create (empty, from a git URL, for a ZIP upload, a server folder, or an Apple app
from Forge's template), list, rename and delete them."""

import logging
import secrets
import time
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from forge_web.access import member_projects, require_project
from forge_web.apple.template import app_name, checked_bundle_id, write_template
from forge_web.audit import audit
from forge_web.auth.sessions import CurrentUser, client_ip
from forge_web.db.models import Project, ProjectMember, User
from forge_web.gitsync import check_remote, clone
from forge_web.quotas import MB, check_project_count, disk_limit, disk_use
from forge_web.services import Services, services_of
from forge_web.sources import server_folder

log = logging.getLogger(__name__)


class ProjectIn(BaseModel):
    """A new project and where its files come from (a ZIP is uploaded once it exists)."""

    name: str = Field(min_length=1, max_length=100)
    source: Literal["empty", "git", "zip", "folder", "apple"] = "empty"
    url: str = Field(default="", max_length=2000)  # git
    folder: str = Field(default="", max_length=4096)  # folder (admins)
    bundle_id: str = Field(default="", max_length=155)  # apple (default com.example.<app>)


class ProjectPatch(BaseModel):
    """Changes to a project."""

    name: str | None = Field(default=None, min_length=1, max_length=100)


class ProjectOut(BaseModel):
    """A project as the web UI sees it."""

    id: str
    name: str
    role: str
    source: str
    kind: str  # code | apple
    created_at: float
    updated_at: float


def project_out(project: Project, role: str) -> ProjectOut:
    """The UI view of a project."""
    return ProjectOut(
        id=project.id,
        name=project.name,
        role=role,
        source=project.source,
        kind=project.kind,
        created_at=project.created_at,
        updated_at=project.updated_at,
    )


def projects_router() -> APIRouter:
    """`/api/projects`."""
    router = APIRouter(prefix="/api/projects")

    @router.get("")
    async def list_projects(request: Request, user: CurrentUser) -> list[ProjectOut]:
        async with services_of(request).db.session() as session:
            return [project_out(p, role) for p, role in await member_projects(session, user)]

    @router.post("", status_code=201)
    async def create_project(body: ProjectIn, request: Request, user: CurrentUser) -> ProjectOut:
        services = services_of(request)
        await check_project_count(services, user)
        folder = await chosen_source(services, user, body)
        bundle_id = (
            checked_bundle_id(app_name(body.name), body.bundle_id) if body.source == "apple" else ""
        )
        now = time.time()
        project = Project(
            id=secrets.token_hex(8), name=body.name.strip(), owner_id=user.id,
            source=body.source, source_url=body.url.strip() if body.source == "git" else "",
            folder=str(folder or ""), kind="apple" if body.source == "apple" else "code",
            created_at=now, updated_at=now,
        )  # fmt: skip
        async with services.db.session() as session, session.begin():
            session.add(project)
            await session.flush()
            session.add(ProjectMember(project_id=project.id, user_id=user.id, role="owner"))
        if folder is not None:
            services.driver.folders[project.id] = folder
        try:
            await fill(services, project, user, client_ip(request))
            if body.source == "apple":
                await write_template(services, project.id, app_name(body.name), bundle_id)
        except HTTPException:
            await delete_project(request, project)
            raise
        except Exception as err:
            log.warning("could not prepare the sandbox of project %s: %s", project.id, err)
            await delete_project(request, project)
            raise HTTPException(503, "the project's sandbox could not be started") from None
        await audit(services.db, "project_created", user_id=user.id, target=project.id,
                    ip=client_ip(request), source=body.source)  # fmt: skip
        return project_out(project, "owner")

    @router.get("/{project_id}")
    async def get_project(project_id: str, request: Request, user: CurrentUser) -> ProjectOut:
        async with services_of(request).db.session() as session:
            project, role = await require_project(session, user, project_id)
            return project_out(project, role)

    @router.patch("/{project_id}")
    async def patch_project(
        project_id: str, body: ProjectPatch, request: Request, user: CurrentUser
    ) -> ProjectOut:
        async with services_of(request).db.session() as session, session.begin():
            project, role = await require_project(session, user, project_id, "owner")
            if body.name is not None:
                project.name = body.name.strip()
            project.updated_at = time.time()
            return project_out(project, role)

    @router.delete("/{project_id}", status_code=204)
    async def remove_project(project_id: str, request: Request, user: CurrentUser) -> None:
        async with services_of(request).db.session() as session:
            project, _ = await require_project(session, user, project_id, "owner")
        await delete_project(request, project)

    return router


async def delete_project(request: Request, project: Project) -> None:
    """Stop the project's sandbox, delete its files and its rows (chats go with it)."""
    services = services_of(request)
    await services.runs.forget_project(project.id)
    await services.driver.remove(project.id)
    async with services.db.session() as session, session.begin():
        row = await session.get(Project, project.id)
        if row is not None:
            await session.delete(row)


async def chosen_source(services: Services, user: User, body: ProjectIn) -> Path | None:
    """Check the source before anything is created; the server folder, if that is the source."""
    if body.source == "folder":
        if user.role != "admin":
            raise HTTPException(403, "only admins can open server folders as projects")
        return server_folder(services.settings, body.folder)
    if body.source == "git":
        await check_remote(services, body.url.strip())
    return None


async def fill(services: Services, project: Project, user: User, ip: str) -> None:
    """Prepare a new project's files: a git repository, cloned if it comes from a URL."""
    if project.source == "folder":
        return  # an admin's folder stays as it is
    await services.runs.call(project.id, "git.init")
    if project.source == "git":
        await clone(services, project.id, user, project.source_url, ip)
        limit = disk_limit(services)
        used = await disk_use(services, project.id, fresh=True)
        if limit is not None and used > limit:
            raise HTTPException(
                413, f"the repository needs {used // MB} MB; projects may use {limit // MB} MB"
            )
