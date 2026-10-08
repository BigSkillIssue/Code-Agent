"""Projects: create, list, rename and delete them (more sources follow in W12)."""

import logging
import secrets
import time

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from forge_web.access import member_projects, require_project
from forge_web.auth.dev import CurrentUser
from forge_web.db.models import Project, ProjectMember
from forge_web.services import services_of

log = logging.getLogger(__name__)


class ProjectIn(BaseModel):
    """A new project."""

    name: str = Field(min_length=1, max_length=100)


class ProjectPatch(BaseModel):
    """Changes to a project."""

    name: str | None = Field(default=None, min_length=1, max_length=100)


class ProjectOut(BaseModel):
    """A project as the web UI sees it."""

    id: str
    name: str
    role: str
    source: str
    created_at: float
    updated_at: float


def project_out(project: Project, role: str) -> ProjectOut:
    """The UI view of a project."""
    return ProjectOut(
        id=project.id,
        name=project.name,
        role=role,
        source=project.source,
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
        now = time.time()
        project = Project(
            id=secrets.token_hex(8), name=body.name.strip(), owner_id=user.id,
            created_at=now, updated_at=now,
        )  # fmt: skip
        async with services.db.session() as session, session.begin():
            session.add(project)
            await session.flush()
            session.add(ProjectMember(project_id=project.id, user_id=user.id, role="owner"))
        try:
            await services.runs.call(project.id, "git.init")
        except Exception as err:
            log.warning("could not prepare the sandbox of project %s: %s", project.id, err)
            await delete_project(request, project)
            raise HTTPException(503, "the project's sandbox could not be started") from None
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
