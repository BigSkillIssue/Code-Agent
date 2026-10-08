"""A project's repository over HTTP: status, stage, commit, discard, branches, log, remote."""

import subprocess
import sys
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import httpx
import pytest

from forge_web.db.models import ProjectMember
from forge_web.git_api import remote_problem
from support import LiveServer, dev_settings, person

POSIX_ONLY = pytest.mark.skipif(sys.platform == "win32", reason="shell hooks need POSIX")


@pytest.fixture
def server(tmp_path: Path) -> Iterator[LiveServer]:
    with LiveServer(dev_settings(tmp_path / "data")) as live:
        yield live


@pytest.fixture
async def client(server: LiveServer) -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(base_url=server.url, headers=server.headers(), timeout=60) as c:
        yield c


async def project(client: httpx.AsyncClient) -> tuple[str, str]:
    created = await client.post("/api/projects", json={"name": "Repo"})
    pid = created.json()["id"]
    return pid, f"/api/projects/{pid}"


def git(workspace: Path, *args: str) -> str:
    done = subprocess.run(["git", *args], cwd=workspace, capture_output=True, text=True, check=True)
    return done.stdout


def test_remote_urls() -> None:
    assert remote_problem("https://github.com/ada/app.git") is None
    assert remote_problem("http://github.com/ada/app.git")
    assert remote_problem("git@github.com:ada/app.git")
    assert remote_problem("https://ada:ghp_secret@github.com/ada/app.git")
    assert remote_problem("https://github.com/ada/app.git\n[core]")


async def test_stage_commit_discard_branches(server: LiveServer, client: httpx.AsyncClient) -> None:
    pid, base = await project(client)
    workspace = server.services.driver.workspace(pid)
    for path, text in (("app.py", "a = 1\n"), ("notes.md", "notes\n")):
        await client.put(f"{base}/files/content", json={"path": path, "text": text})
    status = (await client.get(f"{base}/git/status")).json()
    assert {f["path"]: f["worktree"] for f in status["files"]} == {"app.py": "?", "notes.md": "?"}
    nothing = await client.post(f"{base}/git/commit", json={"message": "empty"})
    assert nothing.status_code == 409
    assert (await client.post(f"{base}/git/stage", json={"paths": ["app.py"]})).status_code == 200
    committed = await client.post(f"{base}/git/commit", json={"message": "Add app\n\nFirst."})
    assert committed.status_code == 200, committed.text
    log = (await client.get(f"{base}/git/log")).json()["commits"]
    assert [(c["subject"], c["author"], c["email"]) for c in log] == [
        ("Add app", "Admin", "admin@localhost")
    ]
    (workspace / "app.py").write_text("a = 2\n")
    diff = (await client.get(f"{base}/git/diff", params={"path": "app.py"})).json()["diff"]
    assert "+a = 2" in diff
    discard = await client.post(f"{base}/git/discard", json={"paths": ["app.py", "notes.md"]})
    assert discard.status_code == 200
    assert (workspace / "app.py").read_text() == "a = 1\n"
    assert not (workspace / "notes.md").exists()
    switched = await client.post(f"{base}/git/switch", json={"branch": "feature/x", "create": True})
    assert switched.status_code == 200
    branches = (await client.get(f"{base}/git/branches")).json()
    assert branches["current"] == "feature/x"
    assert {b["name"] for b in branches["branches"]} == {"main", "feature/x"}
    bad = await client.post(f"{base}/git/switch", json={"branch": "-d", "create": True})
    assert bad.status_code == 409


@POSIX_ONLY
async def test_commits_never_run_repository_hooks(
    server: LiveServer, client: httpx.AsyncClient, tmp_path: Path
) -> None:
    pid, base = await project(client)
    workspace = server.services.driver.workspace(pid)
    marker = tmp_path / "hook-ran"
    for folder in (workspace / ".git" / "hooks", workspace / "evil-hooks"):
        folder.mkdir(exist_ok=True)
        for name in ("pre-commit", "commit-msg", "post-commit", "prepare-commit-msg"):
            hook = folder / name
            hook.write_text(f"#!/bin/sh\necho {name} >> {marker}\n")
            hook.chmod(0o755)
    git(workspace, "config", "core.hooksPath", str(workspace / "evil-hooks"))
    await client.put(f"{base}/files/content", json={"path": "a.txt", "text": "a\n"})
    await client.post(f"{base}/git/stage", json={"paths": ["."]})
    committed = await client.post(f"{base}/git/commit", json={"message": "no hooks"})
    assert committed.status_code == 200, committed.text
    assert not marker.exists()


async def test_the_remote_is_checked(server: LiveServer, client: httpx.AsyncClient) -> None:
    pid, base = await project(client)
    assert (await client.get(f"{base}/git/remote")).json() == {"url": None, "problem": None}
    for bad in ("http://github.com/a/b.git", "https://a:token@github.com/a/b.git"):
        assert (await client.put(f"{base}/git/remote", json={"url": bad})).status_code == 422
    good = "https://github.com/ada/app.git"
    assert (await client.put(f"{base}/git/remote", json={"url": good})).status_code == 200
    assert (await client.get(f"{base}/git/remote")).json() == {"url": good, "problem": None}
    workspace = server.services.driver.workspace(pid)
    assert git(workspace, "remote", "get-url", "origin").strip() == good


async def test_viewers_see_but_do_not_change(server: LiveServer, client: httpx.AsyncClient) -> None:
    pid, base = await project(client)
    await client.put(f"{base}/files/content", json={"path": "a.txt", "text": "a\n"})
    viewer = await person(server, "viewer")
    async with server.services.db.session() as session, session.begin():
        session.add(ProjectMember(project_id=pid, user_id=viewer.id, role="viewer"))
    assert (await viewer.web.get(f"{base}/git/status")).status_code == 200
    changes = {"stage": {"paths": ["."]}, "commit": {"message": "x"},
               "discard": {"paths": ["a.txt"]}, "switch": {"branch": "b"}}  # fmt: skip
    for route, body in changes.items():
        assert (await viewer.web.post(f"{base}/git/{route}", body)).status_code == 403
    remote = await viewer.web.request("PUT", f"{base}/git/remote", json={"url": "https://x.y/z"})
    assert remote.status_code == 403
