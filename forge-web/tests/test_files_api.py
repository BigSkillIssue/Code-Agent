"""A project's files over HTTP: read and change them, upload and download any size, roles."""

import os
import sys
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import httpx
import pytest

from forge_web.db.models import ProjectMember
from support import LiveServer, Person, dev_settings, person

POSIX_ONLY = pytest.mark.skipif(sys.platform == "win32", reason="symlinks need POSIX")


@pytest.fixture
def server(tmp_path: Path) -> Iterator[LiveServer]:
    with LiveServer(dev_settings(tmp_path / "data")) as live:
        yield live


@pytest.fixture
async def client(server: LiveServer) -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(base_url=server.url, headers=server.headers(), timeout=60) as c:
        yield c


async def new_project(client: httpx.AsyncClient) -> str:
    created = await client.post("/api/projects", json={"name": "Files"})
    assert created.status_code == 201, created.text
    return str(created.json()["id"])


async def add_viewer(server: LiveServer, project_id: str) -> Person:
    viewer = await person(server, "viewer")
    async with server.services.db.session() as session, session.begin():
        session.add(ProjectMember(project_id=project_id, user_id=viewer.id, role="viewer"))
    return viewer


async def test_list_read_save_rename_delete(server: LiveServer, client: httpx.AsyncClient) -> None:
    pid = await new_project(client)
    base = f"/api/projects/{pid}/files"
    saved = await client.put(f"{base}/content", json={"path": "src/app.py", "text": "a = 1\n"})
    assert saved.status_code == 200, saved.text
    listing = (await client.get(f"{base}/list", params={"path": "src"})).json()
    assert [e["name"] for e in listing["entries"]] == ["app.py"]
    read = (await client.get(f"{base}/content", params={"path": "src/app.py"})).json()
    assert read["text"] == "a = 1\n" and not read["binary"]
    stale = {"path": "src/app.py", "text": "a = 2\n", "expected_mtime": read["mtime"] - 10}
    assert (await client.put(f"{base}/content", json=stale)).status_code == 409
    fresh = {**stale, "expected_mtime": read["mtime"]}
    assert (await client.put(f"{base}/content", json=fresh)).status_code == 200
    assert (await client.post(f"{base}/mkdir", json={"path": "docs"})).status_code == 200
    moved = await client.post(f"{base}/rename", json={"src": "src/app.py", "dst": "docs/app.py"})
    assert moved.status_code == 200
    workspace = server.services.driver.workspace(pid)
    assert (workspace / "docs" / "app.py").read_text() == "a = 2\n"
    gone = await client.delete(base, params={"path": "docs", "recursive": "true"})
    assert gone.status_code == 204 and not (workspace / "docs").exists()
    assert (await client.get(f"{base}/content", params={"path": "nope.txt"})).status_code == 404
    escape = await client.put(f"{base}/content", json={"path": "../outside.txt", "text": "x"})
    assert escape.status_code == 400 and not (workspace.parent / "outside.txt").exists()


@POSIX_ONLY
async def test_links_are_not_followed(server: LiveServer, client: httpx.AsyncClient) -> None:
    pid = await new_project(client)
    workspace = server.services.driver.workspace(pid)
    secret = workspace.parent / "secret.txt"
    secret.write_text("outside the workspace")
    os.symlink(secret, workspace / "link.txt")
    base = f"/api/projects/{pid}/files"
    assert (await client.get(f"{base}/content", params={"path": "link.txt"})).status_code == 400
    assert (await client.get(f"{base}/raw", params={"path": "link.txt"})).status_code == 400
    overwrite = await client.put(f"{base}/content", json={"path": "link.txt", "text": "x"})
    assert overwrite.status_code == 400 and secret.read_text() == "outside the workspace"


async def test_large_uploads_and_downloads(server: LiveServer, client: httpx.AsyncClient) -> None:
    pid = await new_project(client)
    base = f"/api/projects/{pid}/files"
    data = os.urandom(1_700_000)  # several parts
    put = await client.put(f"{base}/raw", params={"path": "assets/blob.bin"}, content=data)
    assert put.status_code == 200 and put.json()["size"] == len(data)
    got = await client.get(f"{base}/raw", params={"path": "assets/blob.bin"})
    assert got.status_code == 200 and got.content == data
    assert got.headers["content-type"] == "application/octet-stream"
    assert got.headers["content-disposition"].startswith("attachment;")
    assert got.headers["x-content-type-options"] == "nosniff"
    assert got.headers["content-security-policy"] == "sandbox"
    workspace = server.services.driver.workspace(pid)
    assert [p.name for p in (workspace / "assets").iterdir()] == ["blob.bin"]
    page = b"<html><script>alert(document.cookie)</script></html>"
    await client.put(f"{base}/raw", params={"path": "page.html"}, content=page)
    html = await client.get(f"{base}/raw", params={"path": "page.html"})
    assert html.content == page and html.headers["content-type"] == "application/octet-stream"


async def test_uploads_are_limited(server: LiveServer, client: httpx.AsyncClient) -> None:
    pid = await new_project(client)
    server.services.settings.server.max_upload_mb = 1
    too_big = await client.put(
        f"/api/projects/{pid}/files/raw", params={"path": "big.bin"}, content=b"x" * 1_600_000
    )
    assert too_big.status_code == 413
    workspace = server.services.driver.workspace(pid)
    assert sorted(p.name for p in workspace.iterdir()) == [".git"]  # no file, no leftover part


async def test_viewers_read_but_do_not_change(
    server: LiveServer, client: httpx.AsyncClient
) -> None:
    pid = await new_project(client)
    base = f"/api/projects/{pid}/files"
    await client.put(f"{base}/content", json={"path": "notes.md", "text": "hello\n"})
    viewer = (await add_viewer(server, pid)).web
    assert (await viewer.get(f"{base}/content?path=notes.md")).json()["text"] == "hello\n"
    assert (await viewer.get(f"{base}/raw?path=notes.md")).status_code == 200
    changes = [
        viewer.request("PUT", f"{base}/content", json={"path": "notes.md", "text": "x"}),
        viewer.request("PUT", f"{base}/raw", params={"path": "x.bin"}, content=b"x"),
        viewer.request("POST", f"{base}/mkdir", json={"path": "d"}),
        viewer.request("POST", f"{base}/rename", json={"src": "notes.md", "dst": "n.md"}),
        viewer.request("DELETE", base, params={"path": "notes.md"}),
    ]
    for change in changes:
        assert (await change).status_code == 403
    workspace = server.services.driver.workspace(pid)
    assert (workspace / "notes.md").read_text() == "hello\n"
