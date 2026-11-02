"""A sandboxed command can write `.git/config` (the sandbox cannot protect .git); the git that
Forge itself runs outside the sandbox must not run the programs such a config names."""

import sys
from pathlib import Path

import pytest

from forge.context import git_status
from forge.runtime.checkpoint import restore, snapshot
from forge.runtime.gitops import changed_files, diff_since
from forge.runtime.ignore import project_files
from forge.runtime.worktree import apply_merge, create_worktree, prepare_merge
from support import git, make_ctx

pytestmark = pytest.mark.skipif(
    sys.platform == "win32", reason="the planted commands are POSIX shell"
)


@pytest.fixture
def planted(tmp_project: Path, tmp_path: Path) -> Path:
    """A repository whose config runs a program for every hook git offers; returns the folder
    where each program leaves a marker file."""
    marks = tmp_path / "marks"
    marks.mkdir()
    (tmp_project / "a.txt").write_text("a\n")
    git(tmp_project, "add", "-A")
    git(tmp_project, "commit", "-q", "-m", "start")
    hooks = tmp_path / "hooks"
    hooks.mkdir()
    for hook in ("post-commit", "pre-commit", "post-checkout", "reference-transaction"):
        (hooks / hook).write_text(f"#!/bin/sh\ntouch {marks.as_posix()}/hook-{hook}\n")
        (hooks / hook).chmod(0o755)

    def touch(name: str, then: str = "cat") -> str:
        return f'"touch {marks.as_posix()}/{name}; {then}"'

    config = tmp_project / ".git" / "config"
    config.write_text(
        config.read_text()
        + f"[core]\n\tfsmonitor = {touch('fsmonitor', 'false')}\n\thooksPath = {hooks}\n"
        + f'[filter "evil"]\n\tclean = {touch("clean")}\n\tsmudge = {touch("smudge")}\n'
        + f'[diff "evil"]\n\ttextconv = {touch("textconv")}\n'
        + f"[diff]\n\texternal = {touch('external', 'true')}\n"
        + f'[merge "evil"]\n\tdriver = {touch("merge", "true")}\n'
    )
    info = tmp_project / ".git" / "info"
    info.mkdir(exist_ok=True)
    (info / "attributes").write_text("* filter=evil diff=evil merge=evil\n")
    (tmp_project / "a.txt").write_text("a\nchanged\n")
    (tmp_project / "new.txt").write_text("new\n")
    return marks


async def test_snapshots_and_undo_run_nothing_from_the_repo_config(
    tmp_project: Path, planted: Path
) -> None:
    ref = await snapshot(tmp_project, "s", "s1")
    assert ref is not None
    (tmp_project / "a.txt").write_text("later\n")
    await restore(tmp_project, ref)
    assert (tmp_project / "a.txt").read_text() == "a\nchanged\n"
    assert sorted(p.name for p in planted.iterdir()) == []


async def test_reviews_and_status_run_nothing_from_the_repo_config(
    tmp_project: Path, planted: Path
) -> None:
    assert "changed" in await diff_since(tmp_project)
    assert "a.txt" in await changed_files(tmp_project)
    assert "a.txt" in await git_status(make_ctx(tmp_project))
    assert any(p.name == "new.txt" for p in await project_files(tmp_project))
    assert sorted(p.name for p in planted.iterdir()) == []


async def test_worktrees_and_merges_run_nothing_from_the_repo_config(
    tmp_project: Path, planted: Path
) -> None:
    tree = await create_worktree(tmp_project, "s", "a1")
    (tree.path / "a.txt").write_text("a\nchanged\nby the agent\n")
    prepared = await prepare_merge(tmp_project, tree, "work")
    assert prepared.ok
    await apply_merge(tmp_project, tree, prepared)
    assert (tmp_project / "a.txt").read_text() == "a\nchanged\nby the agent\n"
    assert sorted(p.name for p in planted.iterdir()) == []
