"""Where projects come from (empty, a git URL, a ZIP upload, a server folder) and their quotas."""

import io
import subprocess
import zipfile
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import httpx
import pytest

from support import LiveServer, dev_settings, person


@pytest.fixture
def server(tmp_path: Path) -> Iterator[LiveServer]:
    with LiveServer(dev_settings(tmp_path / "data")) as live:
        live.services.settings.git.allow_local_remotes = True
        yield live


@pytest.fixture
async def client(server: LiveServer) -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(base_url=server.url, headers=server.headers(), timeout=60) as c:
        yield c


def git(folder: Path, *args: str) -> str:
    done = subprocess.run(["git", *args], cwd=folder, capture_output=True, text=True, check=True)
    return done.stdout.strip()


def zip_bytes(entries: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in entries.items():
            zf.writestr(name, data)
    return buffer.getvalue()


async def project_names(client: httpx.AsyncClient) -> list[str]:
    return [p["name"] for p in (await client.get("/api/projects")).json()]


async def test_a_project_from_a_git_url(
    server: LiveServer, client: httpx.AsyncClient, tmp_path: Path
) -> None:
    upstream = tmp_path / "upstream"
    upstream.mkdir()
    git(upstream, "init", "-q", "-b", "trunk")
    (upstream / "README.md").write_text("# hello\n")
    git(upstream, "add", ".")
    git(upstream, "-c", "user.name=U", "-c", "user.email=u@x.y", "commit", "-q", "-m", "init")
    git(upstream, "branch", "other")
    url = f"file://{upstream}"
    created = await client.post(
        "/api/projects", json={"name": "Cloned", "source": "git", "url": url}
    )
    assert created.status_code == 201, created.text
    project = created.json()
    assert project["source"] == "git"
    workspace = server.services.driver.workspace(project["id"])
    assert (workspace / "README.md").read_text() == "# hello\n"
    assert git(workspace, "branch", "--show-current") == "trunk"  # the remote's default
    assert git(workspace, "remote", "get-url", "origin") == url
    assert "origin/other" in git(workspace, "branch", "-r")
    assert ".forge/audit.log" in (workspace / ".git" / "info" / "exclude").read_text()
    assert not (workspace / ".git" / "forge-transfer" / "clone.bundle").exists()


async def test_a_failed_clone_leaves_nothing(server: LiveServer, client: httpx.AsyncClient) -> None:
    body = {"name": "Broken", "source": "git", "url": "file:///does/not/exist"}
    failed = await client.post("/api/projects", json=body)
    assert failed.status_code == 502
    assert await project_names(client) == []
    https = {"name": "Private", "source": "git", "url": "https://10.0.0.1/repo.git"}
    server.services.settings.git.allow_local_remotes = False
    assert (await client.post("/api/projects", json=https)).status_code == 403  # not public
    assert await project_names(client) == []


async def test_a_zip_upload(server: LiveServer, client: httpx.AsyncClient) -> None:
    created = await client.post("/api/projects", json={"name": "Zipped", "source": "zip"})
    pid = created.json()["id"]
    archive = zip_bytes({"site-main/index.html": b"<h1>hi</h1>", "site-main/css/a.css": b"a{}"})
    imported = await client.put(f"/api/projects/{pid}/import/zip", content=archive)
    assert imported.status_code == 200, imported.text
    assert imported.json()["files"] == 2 and imported.json()["root"] == "site-main"
    workspace = server.services.driver.workspace(pid)
    assert (workspace / "css" / "a.css").read_text() == "a{}"
    evil = zip_bytes({"ok.txt": b"ok", "../../evil.txt": b"pwned"})
    refused = await client.put(f"/api/projects/{pid}/import/zip", content=evil)
    assert refused.status_code == 422
    assert not (workspace / "ok.txt").exists() and not (workspace.parent / "evil.txt").exists()
    assert not list(workspace.glob(".forge-web-import*"))  # the archive is removed either way


async def test_server_folders_for_admins_under_allowed_roots(
    server: LiveServer, client: httpx.AsyncClient, tmp_path: Path
) -> None:
    roots = tmp_path / "shared"
    (roots / "app").mkdir(parents=True)
    (roots / "app" / "main.py").write_text("print(1)\n")
    (tmp_path / "elsewhere").mkdir()
    body = {"name": "Shared", "source": "folder", "folder": str(roots / "app")}
    assert (await client.post("/api/projects", json=body)).status_code == 403  # no roots yet
    server.services.settings.sandbox.folder_roots = [str(roots), str(tmp_path / "data")]
    created = await client.post("/api/projects", json=body)
    assert created.status_code == 201, created.text
    pid = created.json()["id"]
    files = (await client.get(f"/api/projects/{pid}/files/list")).json()["entries"]
    assert [e["name"] for e in files] == ["main.py"]
    sneaky = roots / "app" / ".." / ".." / "elsewhere"
    comma = roots / "a,type=volume,source=forge-web-other-workspace"  # docker's --mount syntax
    comma.mkdir()
    for bad, code in ((tmp_path / "elsewhere", 403), (sneaky, 403), (tmp_path / "data", 403),
                      (roots / "missing", 422), (comma, 422)):  # fmt: skip
        attempt = await client.post("/api/projects", json={**body, "folder": str(bad)})
        assert attempt.status_code == code, (bad, attempt.text)
    member = await person(server, "member")
    assert (await member.web.post("/api/projects", body)).status_code == 403
    assert (await client.delete(f"/api/projects/{pid}")).status_code == 204
    assert (roots / "app" / "main.py").read_text() == "print(1)\n"  # the folder stays


async def test_quotas(server: LiveServer, client: httpx.AsyncClient) -> None:
    quotas = server.services.settings.quotas
    quotas.projects_per_user, quotas.project_disk_mb = 1, 1
    member = await person(server, "member")
    assert (await member.web.post("/api/projects", {"name": "One"})).status_code == 201
    second = await member.web.post("/api/projects", {"name": "Two"})
    assert second.status_code == 403 and "limit" in second.json()["detail"]
    pid = (await client.post("/api/projects", json={"name": "Admin 1"})).json()["id"]
    assert (await client.post("/api/projects", json={"name": "Admin 2"})).status_code == 201
    upload = await client.put(
        f"/api/projects/{pid}/files/raw", params={"path": "big.bin"}, content=b"x" * 1_500_000
    )
    assert upload.status_code == 413
    workspace = server.services.driver.workspace(pid)
    (workspace / "big.bin").write_bytes(b"x" * 1_500_000)  # e.g. the agent wrote it
    server.services.disk_use.clear()
    chat = (await client.post(f"/api/projects/{pid}/chats", json={})).json()
    sent = await client.post(f"/api/chats/{chat['id']}/messages", json={"text": "hi"})
    assert sent.status_code == 409 and "MB" in sent.json()["detail"]
    usage = (await client.get(f"/api/projects/{pid}/usage")).json()
    assert usage["bytes"] >= 1_500_000 and usage["limit"] == 1024 * 1024
