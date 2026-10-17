"""Windows apps on the products' websites (W39): a built release goes onto the website after the
admin's approval (trusted users need none), the app's host fetches exactly its files and checks
them, the page at `/downloads` offers the installer and the ZIP, a newer release replaces the
older one, and a withdrawn release is gone from the host; another host sees none of it."""

import base64
import hashlib
import json
import secrets
import time
from pathlib import Path
from typing import Any

import httpx
import pytest
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey

from forge_hostworker.downloads import MANIFEST, Downloads
from forge_web.db.hosting_models import Deploy, HostedApp
from forge_web.db.store_models import WindowsBuild
from forge_web.hosting.hosts import create_host
from support import LiveServer, person
from test_hosting_deploy import approve_head, settings_for, short_polls
from test_winworker import velopack_release, windows_project

__all__ = ["short_polls"]


@pytest.fixture
def server(tmp_path: Path) -> Any:
    settings = settings_for(tmp_path / "data")
    settings.stores.windows_enabled = True
    with LiveServer(settings) as live:
        yield live


async def host(server: LiveServer, name: str) -> tuple[str, str]:
    """A host and its token."""
    key = X25519PrivateKey.generate().public_key().public_bytes_raw()
    row, token = await create_host(server.services.db, name, base64.b64encode(key).decode())
    return row.id, token


async def live_app(server: LiveServer, project_id: str, host_id: str, app: str = "shop") -> None:
    """The project hosted as `app` on that host, live in production."""
    now = time.time()
    async with server.services.db.session() as session, session.begin():
        session.add(HostedApp(project_id=project_id, app=app, host_id=host_id, created_at=now))
        await session.flush()
        session.add(Deploy(id=secrets.token_hex(16), project_id=project_id, app=app,
                           user_id=server.services.dev_user_id, host_id=host_id,
                           environment="production", commit="c", release=1, step="done",
                           status="live", created_at=now, updated_at=now))  # fmt: skip


async def built(server: LiveServer, project_id: str, number: int, signed: bool = False) -> str:
    """A build of the approved head with its release kept, as the workers would leave it."""
    commit = await approve_head(server, project_id, server.services.dev_user_id)
    builds = server.services.windows
    row = await builds.add(project_id, server.services.dev_user_id, commit)
    folder = builds.release(row)
    velopack_release(folder, "Shop", row.version, signed)
    files = {p.name: {"size": p.stat().st_size,
                      "sha256": hashlib.sha256(p.read_bytes()).hexdigest().upper()}
             for p in folder.iterdir()}  # fmt: skip
    await builds.update(row.id, status="built", pack_id="Shop", signed=signed,
                        files=json.dumps(files), file_name="Shop-win-Setup.exe")  # fmt: skip
    assert row.build_number == number
    return row.id


def admin_client(server: LiveServer) -> httpx.AsyncClient:
    return httpx.AsyncClient(base_url=server.url, headers=server.headers(), timeout=60)


def as_host(server: LiveServer, token: str) -> httpx.AsyncClient:
    return httpx.AsyncClient(base_url=server.url, timeout=30,
                             headers={"Authorization": f"Bearer {token}"})  # fmt: skip


async def test_a_release_goes_onto_the_website_after_the_admins_approval(
    server: LiveServer, tmp_path: Path
) -> None:
    async with admin_client(server) as admin:
        project_id = await windows_project(server, admin)
        maker = await person(server, "maker")
        await admin.post(f"/api/projects/{project_id}/members",
                         json={"email": maker.email, "role": "editor"})  # fmt: skip
        first = await built(server, project_id, 1)
        url = f"/api/projects/{project_id}/windows/releases"
        refused = await maker.web.request("POST", url, json={"build_id": first})
        assert refused.status_code == 409 and "not hosted" in refused.text  # no website yet
        host_id, token = await host(server, "host-1")
        await live_app(server, project_id, host_id)
        asked = await maker.web.request("POST", url, json={"build_id": first})
        assert asked.status_code == 200 and asked.json()["status"] == "waiting", asked.text
        async with as_host(server, token) as web:
            assert (await web.get("/api/host/downloads")).json() == {"apps": []}  # not yet
            waiting = (await admin.get("/api/admin/stores/windows/releases")).json()
            assert [r["id"] for r in waiting] == [asked.json()["id"]]
            ok = await admin.post(f"/api/admin/stores/windows/releases/{waiting[0]['id']}/approve")
            assert ok.status_code == 200 and ok.json()["status"] == "published"
            listed = (await web.get("/api/host/downloads")).json()["apps"]
            assert [a["app"] for a in listed] == ["shop"]
            assert {f["name"] for f in listed[0]["files"]} >= {"Shop-win-Setup.exe",
                                                               "Shop-1.0.1-full.nupkg"}  # fmt: skip
            downloads = Downloads(web, tmp_path / "downloads")
            await downloads.sync()
        folder = downloads.offered("shop")
        assert folder is not None and (folder / "Shop-win-Setup.exe").read_bytes()[:2] == b"MZ"
        assert "Shop-1.0.1-full.nupkg" in json.loads((folder / MANIFEST).read_text())
        page = await maker.web.request("GET", "/legal/shop/downloads")
        assert page.status_code == 200
        assert 'href="/downloads/windows/Shop-win-Setup.exe"' in page.text
        assert "Installer herunterladen" in page.text and "nicht digital signiert" in page.text
        assert "Version 1.0.1" in page.text and "Impressum" in page.text


async def test_a_newer_release_replaces_the_older_and_a_withdrawn_one_is_gone(
    server: LiveServer, tmp_path: Path
) -> None:
    async with admin_client(server) as admin:
        project_id = await windows_project(server, admin)
        host_id, token = await host(server, "host-1")
        _, other = await host(server, "host-2")
        await live_app(server, project_id, host_id)
        url = f"/api/projects/{project_id}/windows/releases"
        first = await admin.post(url, json={"build_id": await built(server, project_id, 1)})
        assert first.json()["status"] == "published"  # an admin needs no approval
        second = await admin.post(url, json={"build_id": await built(server, project_id, 2, True)})
        rows = {r["id"]: r for r in (await admin.get(url)).json()}
        assert rows[first.json()["id"]]["status"] == "replaced"
        assert rows[second.json()["id"]]["signed"] is True
        async with as_host(server, token) as web, as_host(server, other) as stranger:
            downloads = Downloads(web, tmp_path / "downloads")
            await downloads.sync()
            assert (tmp_path / "downloads/shop/windows/Shop-1.0.2-full.nupkg").is_file()
            assert not (tmp_path / "downloads/shop/windows/Shop-1.0.1-full.nupkg").exists()
            page = (await admin.get("/legal/shop/downloads")).text
            assert "Version 1.0.2" in page and "nicht digital signiert" not in page
            assert (await stranger.get("/api/host/downloads")).json() == {"apps": []}
            file = "/api/host/downloads/shop/Shop-win-Setup.exe"
            assert (await stranger.get(file)).status_code == 404  # only the app's own host
            assert (await web.get(file)).status_code == 200
            gone = await admin.post(f"{url}/{second.json()['id']}/withdraw")
            assert gone.status_code == 200 and gone.json()["status"] == "withdrawn"
            await downloads.sync()
            assert downloads.offered("shop") is None and not (tmp_path / "downloads/shop").exists()
        assert (await admin.get("/legal/shop/downloads")).status_code == 404


async def test_a_file_that_is_not_what_forge_web_says_is_not_kept(
    server: LiveServer, tmp_path: Path
) -> None:
    async with admin_client(server) as admin:
        project_id = await windows_project(server, admin)
        host_id, token = await host(server, "host-1")
        await live_app(server, project_id, host_id)
        build_id = await built(server, project_id, 1)
        url = f"/api/projects/{project_id}/windows/releases"
        assert (await admin.post(url, json={"build_id": build_id})).status_code == 200
        async with server.services.db.session() as session:
            row = await session.get(WindowsBuild, build_id)
        assert row is not None
        (server.services.windows.release(row) / "Shop-win-Setup.exe").write_bytes(b"MZ changed")
        async with as_host(server, token) as web:
            downloads = Downloads(web, tmp_path / "downloads")
            await downloads.sync()
        assert downloads.offered("shop") is None  # nothing half-checked is ever served
        assert not list((tmp_path / "downloads").glob("shop/windows/*"))
