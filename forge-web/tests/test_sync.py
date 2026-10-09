"""Bringing Forge's core from main into the Forge Web branch: merged, checked, pushed only when
the checks pass (`scripts/sync-core.sh`, run by the sync workflow after each push to main)."""

import os
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).parents[1] / "scripts" / "sync-core.sh"
BRANCH = "forge-web"

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="the sync script is bash")

IDENTITY = {
    "GIT_AUTHOR_NAME": "Test", "GIT_AUTHOR_EMAIL": "test@example.com",
    "GIT_COMMITTER_NAME": "Test", "GIT_COMMITTER_EMAIL": "test@example.com",
}  # fmt: skip


def git(cwd: Path, *args: str) -> str:
    done = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True,
                          env={**os.environ, **IDENTITY}, check=True)  # fmt: skip
    return done.stdout.strip()


def commit(repo: Path, path: str, text: str, message: str) -> None:
    (repo / path).parent.mkdir(parents=True, exist_ok=True)
    (repo / path).write_text(text)
    git(repo, "add", path)
    git(repo, "commit", "-q", "-m", message)


@pytest.fixture
def repos(tmp_path: Path) -> tuple[Path, Path, Path]:
    """A remote with main and the Forge Web branch, a clone on that branch, a clone on main."""
    remote = tmp_path / "remote.git"
    git(tmp_path, "init", "-q", "--bare", "-b", "main", str(remote))
    core = tmp_path / "core"
    git(tmp_path, "clone", "-q", str(remote), str(core))
    commit(core, "src/forge/engine.py", "VERSION = 1\n", "core")
    commit(core, "shared.txt", "one\n", "shared")
    git(core, "push", "-q", "origin", "HEAD:main")
    web = tmp_path / "web"
    git(tmp_path, "clone", "-q", str(remote), str(web))
    git(web, "checkout", "-q", "-b", BRANCH)
    commit(web, "forge-web/app.py", "print('web')\n", "web")
    git(web, "push", "-q", "origin", BRANCH)
    return remote, web, core


def sync(web: Path, gate: str = "true") -> subprocess.CompletedProcess[str]:
    env = {**os.environ, **IDENTITY, "SYNC_GATE": gate, "SYNC_BRANCH": BRANCH}
    env.pop("GITHUB_OUTPUT", None)
    return subprocess.run(["bash", str(SCRIPT)], cwd=web, capture_output=True, text=True, env=env)


def on_remote(remote: Path, ref: str) -> str:
    return git(remote, "rev-parse", ref)


def test_nothing_new_on_main_changes_nothing(repos: tuple[Path, Path, Path]) -> None:
    remote, web, _core = repos
    before = on_remote(remote, BRANCH)
    done = sync(web)
    assert done.returncode == 0, done.stderr
    assert "already" in done.stdout and on_remote(remote, BRANCH) == before


def test_core_changes_are_merged_checked_and_pushed(
    repos: tuple[Path, Path, Path], tmp_path: Path
) -> None:
    remote, web, core = repos
    commit(core, "src/forge/engine.py", "VERSION = 2\n", "better core")
    git(core, "push", "-q", "origin", "HEAD:main")
    marker = tmp_path / "gate-ran"
    done = sync(
        web, gate=f"test -f forge-web/app.py && grep -q 2 src/forge/engine.py && touch {marker}"
    )
    assert done.returncode == 0, done.stderr
    assert marker.exists()  # the checks ran on the merged tree
    pushed = on_remote(remote, BRANCH)
    git(web, "fetch", "-q", "origin")
    assert git(web, "merge-base", "--is-ancestor", "origin/main", pushed) == ""
    assert git(web, "show", f"{pushed}:src/forge/engine.py") == "VERSION = 2"
    assert git(web, "show", f"{pushed}:forge-web/app.py") == "print('web')"


def test_a_conflict_pushes_nothing(repos: tuple[Path, Path, Path]) -> None:
    remote, web, core = repos
    commit(core, "shared.txt", "core's line\n", "core edit")
    git(core, "push", "-q", "origin", "HEAD:main")
    commit(web, "shared.txt", "web's line\n", "web edit")
    git(web, "push", "-q", "origin", BRANCH)
    before = on_remote(remote, BRANCH)
    done = sync(web)
    assert done.returncode == 2 and "conflict" in done.stderr
    assert on_remote(remote, BRANCH) == before
    assert git(web, "status", "--porcelain") == ""  # the merge was undone


def test_failing_checks_push_nothing(repos: tuple[Path, Path, Path]) -> None:
    remote, web, core = repos
    commit(core, "src/forge/engine.py", "VERSION = 'broken'\n", "breaking core")
    git(core, "push", "-q", "origin", "HEAD:main")
    before = on_remote(remote, BRANCH)
    done = sync(web, gate="false")
    assert done.returncode == 3 and "checks fail" in done.stderr
    assert on_remote(remote, BRANCH) == before
    assert sync(web, gate="false").returncode == 3  # and a second run tries again


def test_local_changes_are_left_alone(repos: tuple[Path, Path, Path]) -> None:
    _remote, web, _core = repos
    (web / "forge-web" / "app.py").write_text("print('unsaved')\n")
    done = sync(web)
    assert done.returncode == 4 and "commit or stash" in done.stderr
    assert (web / "forge-web" / "app.py").read_text() == "print('unsaved')\n"


def test_a_push_while_checking_starts_the_sync_over(
    repos: tuple[Path, Path, Path], tmp_path: Path
) -> None:
    remote, web, core = repos
    commit(core, "src/forge/engine.py", "VERSION = 2\n", "better core")
    git(core, "push", "-q", "origin", "HEAD:main")
    other = tmp_path / "other"
    git(tmp_path, "clone", "-q", "-b", BRANCH, str(remote), str(other))
    flag = tmp_path / "pushed-once"
    # Someone pushes to the branch while the first round of checks runs (seen on GitHub).
    late = (f"touch {flag} && cd {other} && echo late > forge-web/late.py && git add -A"
            f" && git commit -qm late && git push -q origin HEAD:{BRANCH}")  # fmt: skip
    done = sync(web, gate=f"test -f {flag} || ({late})")
    assert done.returncode == 0, done.stderr
    assert "starting over" in done.stderr
    pushed = on_remote(remote, BRANCH)
    assert git(web, "show", f"{pushed}:forge-web/late.py") == "late"  # nothing was lost
    assert git(web, "show", f"{pushed}:src/forge/engine.py") == "VERSION = 2"
