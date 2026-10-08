"""Push and pull through a git job: the project never sees the token, repository hooks never
run, and the remote must be https on an allowed, public host."""

import subprocess
import sys
import tempfile
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import httpx
import pytest
from fastapi import HTTPException

from forge_web.containers.docker import DockerDriver
from forge_web.containers.gitjob import SCRIPT, GitJob
from forge_web.gitsync import check_branch, mask, store_line
from forge_web.settings import SandboxSettings
from support import LiveServer, dev_settings

POSIX_ONLY = pytest.mark.skipif(sys.platform == "win32", reason="git jobs need sh")
HOOKS = ("pre-push", "post-merge", "post-checkout", "reference-transaction", "post-rewrite",
         "pre-commit", "post-commit", "fsmonitor-watchman")  # fmt: skip


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


def remote_repo(tmp_path: Path) -> Path:
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(remote)], check=True)
    return remote


def contributor(tmp_path: Path, remote: Path, name: str, text: str) -> None:
    """Someone else pushes a commit to the remote."""
    other = tmp_path / f"other-{name}"
    if not other.exists():
        subprocess.run(["git", "clone", "-q", str(remote), str(other)], check=True)
        git(other, "config", "user.email", "o@example.com")
        git(other, "config", "user.name", "Other")
    git(other, "pull", "-q", "--ff-only")
    (other / name).write_text(text)
    git(other, "add", ".")
    git(other, "commit", "-q", "-m", f"add {name}")
    git(other, "push", "-q", "origin", "HEAD:main")


async def committed_project(client: httpx.AsyncClient, remote: Path) -> tuple[str, str]:
    pid = (await client.post("/api/projects", json={"name": "Sync"})).json()["id"]
    base = f"/api/projects/{pid}"
    await client.put(f"{base}/files/content", json={"path": "a.txt", "text": "a\n"})
    await client.post(f"{base}/git/stage", json={"paths": ["."]})
    assert (await client.post(f"{base}/git/commit", json={"message": "first"})).status_code == 200
    put = await client.put(f"{base}/git/remote", json={"url": f"file://{remote}"})
    assert put.status_code == 200, put.text
    return pid, base


def test_names_credentials_and_masking() -> None:
    assert check_branch("feature/x-1") == "feature/x-1"
    for bad in ("-x", "a..b", "a b", "/a", "a/", "a//b", "a;rm -rf", ""):
        with pytest.raises(HTTPException):
            check_branch(bad)
    line = store_line("github.com", ("ada", "gh/p:t@x"))
    assert line == "https://ada:gh%2Fp%3At%40x@github.com"
    assert store_line("", ("ada", "t")) == "" and store_line("github.com", None) == ""
    assert mask("fatal: gh/p:t@x and gh%2Fp%3At%40x", ("ada", "gh/p:t@x")) == "fatal: *** and ***"


@POSIX_ONLY
async def test_push_and_pull(server: LiveServer, client: httpx.AsyncClient, tmp_path: Path) -> None:
    remote = remote_repo(tmp_path)
    pid, base = await committed_project(client, remote)
    pushed = await client.post(f"{base}/git/push", json={})
    assert pushed.status_code == 200, pushed.text
    assert git(remote, "log", "--format=%s", "main") == "first"
    workspace = server.services.driver.workspace(pid)
    assert list((workspace / ".git" / "forge-transfer").iterdir()) == []  # bundle consumed
    contributor(tmp_path, remote, "b.txt", "b\n")
    pulled = await client.post(f"{base}/git/pull", json={})
    assert pulled.status_code == 200, pulled.text
    assert pulled.json()["merged"] and (workspace / "b.txt").read_text() == "b\n"
    await client.put(f"{base}/files/content", json={"path": "mine.txt", "text": "m\n"})
    await client.post(f"{base}/git/stage", json={"paths": ["."]})
    await client.post(f"{base}/git/commit", json={"message": "mine"})
    contributor(tmp_path, remote, "theirs.txt", "t\n")
    diverged = (await client.post(f"{base}/git/pull", json={})).json()
    assert diverged["merged"] is False and "both" in diverged["reason"]
    assert git(workspace, "rev-parse", "origin/main") == git(remote, "rev-parse", "main")


@POSIX_ONLY
async def test_hooks_never_run(
    server: LiveServer, client: httpx.AsyncClient, tmp_path: Path
) -> None:
    remote = remote_repo(tmp_path)
    pid, base = await committed_project(client, remote)
    workspace = server.services.driver.workspace(pid)
    marker = tmp_path / "ran"
    for folder in (workspace / ".git" / "hooks", workspace / "evil"):
        folder.mkdir(exist_ok=True)
        for name in HOOKS:
            (folder / name).write_text(f"#!/bin/sh\necho {name} >> {marker}\n")
            (folder / name).chmod(0o755)
    git(workspace, "config", "core.hooksPath", str(workspace / "evil"))
    git(workspace, "config", "core.fsmonitor", str(workspace / "evil" / "fsmonitor-watchman"))
    git(workspace, "config", "credential.helper", f"!{workspace / 'evil' / 'pre-push'}")
    assert (await client.post(f"{base}/git/push", json={})).status_code == 200
    contributor(tmp_path, remote, "b.txt", "b\n")
    assert (await client.post(f"{base}/git/pull", json={})).status_code == 200
    assert not marker.exists()


@POSIX_ONLY
async def test_the_token_stays_out_of_the_project(
    server: LiveServer, client: httpx.AsyncClient, tmp_path: Path
) -> None:
    remote = remote_repo(tmp_path)
    pid, _ = await committed_project(client, remote)
    secret = "ghp_sekret0123456789"
    before = set(Path(tempfile.gettempdir()).glob("forge-web-git-*"))
    job = GitJob("push", f"file://{remote}", "main", "main", local_remotes=True,
                 credentials=f"https://ada:{secret}@github.com")  # fmt: skip
    workspace = server.services.driver.workspace(pid)
    (workspace / ".git" / "forge-transfer").mkdir()
    git(workspace, "bundle", "create", "-q", ".git/forge-transfer/push.bundle", "refs/heads/main")
    code, output = await server.services.driver.run_git_job(pid, job)
    assert code == 0, output
    assert git(remote, "log", "--format=%s", "main") == "first"
    leaked = [p for p in workspace.rglob("*") if p.is_file() and secret.encode() in p.read_bytes()]
    assert leaked == [] and secret not in git(workspace, "config", "--list")
    assert set(Path(tempfile.gettempdir()).glob("forge-web-git-*")) == before  # job folder gone


async def test_remotes_must_be_https_public_and_allowed(
    server: LiveServer, client: httpx.AsyncClient, tmp_path: Path
) -> None:
    pid = (await client.post("/api/projects", json={"name": "Remote"})).json()["id"]
    base = f"/api/projects/{pid}/git"
    server.services.settings.git.allow_local_remotes = False
    local = await client.put(f"{base}/remote", json={"url": f"file://{tmp_path}"})
    assert local.status_code == 422
    assert (await client.post(f"{base}/push", json={"branch": "main"})).status_code == 409
    await client.put(f"{base}/remote", json={"url": "https://127.0.0.1/internal.git"})
    private = await client.post(f"{base}/push", json={"branch": "main"})
    assert private.status_code == 403 and "public" in private.json()["detail"]
    server.services.settings.git.hosts = ["github.com"]
    await client.put(f"{base}/remote", json={"url": "https://gitlab.com/ada/app.git"})
    other = await client.post(f"{base}/push", json={"branch": "main"})
    assert other.status_code == 403 and "gitlab.com" in other.json()["detail"]
    bad_branch = await client.post(f"{base}/push", json={"branch": "main;reboot"})
    assert bad_branch.status_code == 422


def test_the_docker_git_job_is_hardened() -> None:
    driver = DockerDriver(SandboxSettings(image="img:1"))
    job = GitJob(
        "push", "https://github.com/ada/app.git", "main", "main",
        credentials="https://ada:ghp_tok@github.com", pin=("github.com", "140.82.112.3"),
    )  # fmt: skip
    args = driver.git_job_args("abc123", job, "forge-web-abc123-git-1", "runsc")
    joined = " ".join(args)
    for expected in (
        "--rm", "--pull never", "--read-only", "--cap-drop ALL", "--user 1000:1000",
        "--security-opt no-new-privileges", "--add-host github.com:140.82.112.3",
        "source=forge-web-abc123-workspace,target=/workspace", "--runtime runsc",
        "--entrypoint sh",
    ):  # fmt: skip
        assert expected in joined, expected
    assert args[-3:] == ["img:1", "-c", SCRIPT]
    assert "ghp_tok" not in joined  # the token only travels on stdin
    assert "--privileged" not in joined and "docker.sock" not in joined and "-home" not in joined
