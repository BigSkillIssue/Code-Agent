"""Windows apps on the products' websites (W39), instead of a store.

A built release of an approved commit is offered at `https://<app>/downloads` once its maker
asked for it and an admin approved (trusted users and admins need no approval), like a
production deploy. Publishing a release replaces the one before; withdrawing one stops the
downloads until another is published. The app's host serves the files itself: it fetches them
from here with its host token and checks each one's SHA-256, so no product's program is ever
served from Forge Web's own domain. The page that offers them comes from Forge Web, like the
legal pages, so an app cannot change what it says.

- `/api/projects/{id}/windows/releases`: a project's releases; an editor publishes a build and
  withdraws a release.
- `/api/admin/stores/windows/releases`: the releases waiting for an admin; approving and
  withdrawing one.
- `/api/host/downloads`, `/api/host/downloads/{app}/{name}`: what a host's apps offer, and the
  files.
- `/legal/{app}/downloads`: the page, which the edge shows at `/downloads`.
"""

import json
import secrets
import time
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from forge_hostworker.wire import AppDownloads, DownloadFile, DownloadsList
from forge_web.audit import audit
from forge_web.auth.sessions import AdminUser, CurrentUser, client_ip
from forge_web.db.hosting_models import AppOperator, Deploy, GoLiveApproval, HostedApp
from forge_web.db.models import Project, User
from forge_web.db.store_models import WindowsBuild, WindowsRelease
from forge_web.files_api import allowed
from forge_web.hosting.deploy_api import Host
from forge_web.hosting.legal_pages import esc, page
from forge_web.services import Services, services_of
from forge_web.stores.windows import title_of

SHOWN = 20
OFFERED = ("waiting", "published")  # a release that may still be (or is) on the website


class WindowsReleaseIn(BaseModel):
    """Which build goes onto the website."""

    model_config = ConfigDict(extra="forbid")
    build_id: str = Field(pattern=r"^[0-9a-f]{32}$")


def release_view(row: WindowsRelease, build: WindowsBuild | None = None) -> dict[str, Any]:
    """A release as the pages show it."""
    shown = {"id": row.id, "build_id": row.build_id, "status": row.status, "admin": row.admin,
             "admin_approved_at": row.admin_approved_at, "published_at": row.published_at,
             "created_at": row.created_at, "updated_at": row.updated_at}  # fmt: skip
    if build is not None:
        shown |= {"version": build.version, "signed": build.signed, "file_name": build.file_name}
    return shown


def files_of(build: WindowsBuild) -> dict[str, Any]:
    """A build's release files: name -> size and SHA-256."""
    found = json.loads(build.files) if build.files else {}
    return found if isinstance(found, dict) else {}


async def current(
    session: AsyncSession, project_id: str
) -> tuple[WindowsRelease, WindowsBuild] | None:
    """The project's release on its website now, with its build."""
    found = (await session.execute(
        select(WindowsRelease, WindowsBuild)
        .join(WindowsBuild, WindowsBuild.id == WindowsRelease.build_id)
        .where(WindowsRelease.project_id == project_id, WindowsRelease.status == "published")
        .order_by(WindowsRelease.published_at.desc()).limit(1)
    )).first()  # fmt: skip
    return (found[0], found[1]) if found is not None else None


async def published(session: AsyncSession, row: WindowsRelease, now: float) -> None:
    """The release goes onto the website; the one there before is replaced."""
    await session.execute(update(WindowsRelease).where(
        WindowsRelease.project_id == row.project_id, WindowsRelease.status == "published",
        WindowsRelease.id != row.id).values(status="replaced", updated_at=now))  # fmt: skip
    row.status, row.published_at, row.updated_at = "published", now, now


def windows_downloads_router() -> APIRouter:
    """A project's releases, the admins' approval, the hosts' files and the public page."""
    router = APIRouter()
    project_routes(router)
    admin_routes(router)
    host_routes(router)
    page_routes(router)
    return router


def project_routes(router: APIRouter) -> None:
    """`/api/projects/{id}/windows/releases`."""
    url = "/api/projects/{project_id}/windows/releases"

    @router.get(url)
    async def listed(project_id: str, request: Request, user: CurrentUser) -> list[dict[str, Any]]:
        services = await allowed(request, user, project_id)
        async with services.db.session() as session:
            rows = await session.execute(
                select(WindowsRelease, WindowsBuild)
                .join(WindowsBuild, WindowsBuild.id == WindowsRelease.build_id)
                .where(WindowsRelease.project_id == project_id)
                .order_by(WindowsRelease.created_at.desc()).limit(SHOWN)
            )  # fmt: skip
            return [release_view(row, build) for row, build in rows]

    @router.post(url)
    async def publish(
        project_id: str, body: WindowsReleaseIn, request: Request, user: CurrentUser
    ) -> dict[str, Any]:
        services = await allowed(request, user, project_id, "editor")
        row, build = await new_release(services, user, project_id, body.build_id)
        await audit(services.db, "stores.windows_release_started", user_id=user.id,
                    target=project_id, ip=client_ip(request), build=build.id,
                    version=build.version, status=row.status)  # fmt: skip
        return release_view(row, build)

    @router.post(url + "/{release_id}/withdraw")
    async def withdraw(
        project_id: str, release_id: str, request: Request, user: CurrentUser
    ) -> dict[str, Any]:
        services = await allowed(request, user, project_id, "editor")
        row = await withdrawn(services, release_id, user.id, project_id)
        await audit(services.db, "stores.windows_release_withdrawn", user_id=user.id,
                    target=project_id, ip=client_ip(request), release=release_id)  # fmt: skip
        return release_view(row)


async def new_release(
    services: Services, user: User, project_id: str, build_id: str
) -> tuple[WindowsRelease, WindowsBuild]:
    """A release of a built, approved build of an app that is live, once nothing waits."""
    async with services.db.session() as session, session.begin():
        build = await session.get(WindowsBuild, build_id)
        if build is None or build.project_id != project_id:
            raise HTTPException(404, "no such build")
        if build.status != "built" or not files_of(build):
            raise HTTPException(409, "this build has no release to offer")
        approved = await session.scalar(select(GoLiveApproval.id).where(
            GoLiveApproval.project_id == project_id,
            GoLiveApproval.commit == build.commit))  # fmt: skip
        if approved is None:
            raise HTTPException(409, "this build's commit was not approved to go live")
        await require_live(session, project_id)
        waiting = await session.scalar(
            select(WindowsRelease.id).where(
                WindowsRelease.project_id == project_id, WindowsRelease.status == "waiting"
            )
        )
        if waiting is not None:
            raise HTTPException(409, "another release of this app waits for an admin")
        trusted = user.role == "admin" or user.hosting_trusted
        now = time.time()
        row = WindowsRelease(id=secrets.token_hex(16), project_id=project_id, build_id=build.id,
                             user_id=user.id, status="waiting", created_at=now,
                             updated_at=now)  # fmt: skip
        if trusted:
            row.admin = "admin" if user.role == "admin" else "trusted"
            await published(session, row, now)
        session.add(row)
    return row, build


async def require_live(session: AsyncSession, project_id: str) -> None:
    """The app's website must be live in production, and not stopped or being deleted."""
    hosted = await session.get(HostedApp, project_id)
    if hosted is None or hosted.suspended or hosted.deleting_at:
        raise HTTPException(409, "the app is not hosted (or it is stopped)")
    live = await session.scalar(select(Deploy.id).where(
        Deploy.project_id == project_id, Deploy.environment == "production",
        Deploy.status == "live"))  # fmt: skip
    if live is None:
        raise HTTPException(409, "the app is not live in production yet: its website offers "
                            "the downloads")  # fmt: skip


async def withdrawn(
    services: Services, release_id: str, by: str, project_id: str = ""
) -> WindowsRelease:
    """A waiting or published release taken off (404 when it is not the project's)."""
    async with services.db.session() as session, session.begin():
        row = await session.get(WindowsRelease, release_id)
        if row is None or (project_id and row.project_id != project_id):
            raise HTTPException(404, "no such release")
        if row.status not in OFFERED:
            raise HTTPException(409, "this release is not offered")
        row.status, row.withdrawn_by, row.updated_at = "withdrawn", by, time.time()
    return row


def admin_routes(router: APIRouter) -> None:
    """`/api/admin/stores/windows/releases`."""
    url = "/api/admin/stores/windows/releases"

    @router.get(url)
    async def waiting(request: Request, admin: AdminUser) -> list[dict[str, Any]]:
        async with services_of(request).db.session() as session:
            rows = await session.execute(
                select(WindowsRelease, WindowsBuild)
                .join(WindowsBuild, WindowsBuild.id == WindowsRelease.build_id)
                .where(WindowsRelease.status == "waiting").order_by(WindowsRelease.created_at)
            )  # fmt: skip
            return [
                release_view(row, build) | {"project_id": row.project_id, "commit": build.commit}
                for row, build in rows
            ]

    @router.post(url + "/{release_id}/approve")
    async def approve(release_id: str, request: Request, admin: AdminUser) -> dict[str, Any]:
        services = services_of(request)
        async with services.db.session() as session, session.begin():
            row = await session.get(WindowsRelease, release_id)
            if row is None or row.status != "waiting":
                raise HTTPException(409, "this release does not wait for an admin")
            await require_live(session, row.project_id)
            now = time.time()
            row.admin, row.admin_id, row.admin_approved_at = "approved", admin.id, now
            await published(session, row, now)
        await audit(services.db, "stores.windows_release_approved", user_id=admin.id,
                    target=row.project_id, ip=client_ip(request), release=release_id)  # fmt: skip
        return release_view(row)

    @router.post(url + "/{release_id}/withdraw")
    async def withdraw(release_id: str, request: Request, admin: AdminUser) -> dict[str, Any]:
        services = services_of(request)
        row = await withdrawn(services, release_id, admin.id)
        await audit(services.db, "stores.windows_release_withdrawn", user_id=admin.id,
                    target=row.project_id, ip=client_ip(request), release=release_id)  # fmt: skip
        return release_view(row)


async def offers(session: AsyncSession, host_id: str) -> list[AppDownloads]:
    """What the running apps on this host offer for download now."""
    hosted = list(await session.scalars(select(HostedApp).where(
        HostedApp.host_id == host_id, HostedApp.suspended.is_(False),
        HostedApp.deleting_at == 0)))  # fmt: skip
    found = []
    for app in hosted:
        now = await current(session, app.project_id)
        if now is None:
            continue
        files = [DownloadFile(name=name, size=int(f["size"]), sha256=str(f["sha256"]))
                 for name, f in sorted(files_of(now[1]).items())]  # fmt: skip
        found.append(AppDownloads(app=app.app, files=files))
    return found


def host_routes(router: APIRouter) -> None:
    """`/api/host/downloads...`: only the apps on the asking host."""

    @router.get("/api/host/downloads")
    async def listed(host: Host, request: Request) -> DownloadsList:
        async with services_of(request).db.session() as session:
            return DownloadsList(apps=await offers(session, host.id))

    @router.get("/api/host/downloads/{app}/{name}")
    async def file(app: str, name: str, host: Host, request: Request) -> FileResponse:
        services = services_of(request)
        async with services.db.session() as session:
            offered = {entry.app: entry for entry in await offers(session, host.id)}
            build = None
            if app in offered and name in {f.name for f in offered[app].files}:
                hosted = await session.scalar(select(HostedApp).where(HostedApp.app == app))
                now = await current(session, hosted.project_id) if hosted else None
                build = now[1] if now else None
        if build is None:
            raise HTTPException(404, "this host's apps offer no such file")
        path = services.windows.release(build) / name
        if not path.is_file():
            raise HTTPException(404, "the file is gone")
        return FileResponse(path, media_type="application/octet-stream")


def page_routes(router: APIRouter) -> None:
    """`/legal/{app}/downloads`: what visitors of the app's website see at `/downloads`."""

    @router.get("/legal/{app}/downloads", response_class=HTMLResponse)
    async def downloads(app: str, request: Request) -> HTMLResponse:
        async with services_of(request).db.session() as session:
            hosted = await session.scalar(select(HostedApp).where(HostedApp.app == app))
            if hosted is None:
                raise HTTPException(404, "no such app")
            project = await session.get(Project, hosted.project_id)
            operator = await session.get(AppOperator, hosted.project_id)
            now = None if hosted.suspended else await current(session, hosted.project_id)
        title = title_of(project.name) if project else app
        if now is None:
            return page(f"{title} für Windows", "<p>Für diese Anwendung gibt es noch keine "
                        "Windows-Version zum Herunterladen.</p>", status=404)  # fmt: skip
        return page(f"{title} für Windows", download_body(title, now[1], operator))


def size_of(count: int) -> str:
    """Bytes as people read them."""
    return f"{count / 1024 / 1024:.1f} MB".replace(".", ",")


def download_body(title: str, build: WindowsBuild, operator: AppOperator | None) -> str:
    """The downloads page: the installer, the ZIP, what to know, and the checksums."""
    files = files_of(build)
    setup = next(name for name in files if name.endswith("-win-Setup.exe"))
    portable = next(name for name in files if name.endswith("-win-Portable.zip"))
    day = datetime.fromtimestamp(build.updated_at, UTC).strftime("%d.%m.%Y")
    parts = [
        f"<p>Version {esc(build.version)} vom {day}, für Windows 10 und 11 (64 Bit).</p>",
        f'<p><a href="/downloads/windows/{esc(setup)}"><b>Installer herunterladen</b></a> '
        f"({size_of(files[setup]['size'])}): installiert {esc(title)} für Ihr Benutzerkonto, "
        "ohne Administratorrechte, und hält es von selbst aktuell.</p>",
        f'<p><a href="/downloads/windows/{esc(portable)}">Ohne Installation (ZIP)</a> '
        f"({size_of(files[portable]['size'])}): entpacken und „{esc(title)}.exe“ starten; "
        "auch so aktualisiert es sich selbst.</p>",
    ]
    if not build.signed:
        parts.append('<p class="note">Diese Version ist nicht digital signiert. Windows '
                     "SmartScreen kann deshalb beim ersten Start warnen.</p>")  # fmt: skip
    rows = "".join(f"<li>{esc(name)}: <code>{esc(str(f['sha256']))}</code></li>"
                   for name, f in sorted(files.items()) if name in (setup, portable))  # fmt: skip
    parts.append(f'<h2>Prüfsummen (SHA-256)</h2><ul class="note">{rows}</ul>')
    provider = esc(operator.name) if operator else "der Betreiber dieser Anwendung"
    parts.append(f'<p class="note">Anbieter: {provider} · <a href="/impressum">Impressum</a> · '
                 '<a href="/datenschutz">Datenschutz</a></p>')  # fmt: skip
    return "".join(parts)
