"""Git helpers: which files a step changed."""

from pathlib import Path

from forge.runtime.gitops import changed_files


async def test_changed_files_skip_caches(tmp_project: Path) -> None:
    (tmp_project / "calc.py").write_text("x = 1\n")
    for cache in ("__pycache__/calc.cpython-312.pyc", ".pytest_cache/v/cache/lastfailed"):
        path = tmp_project / cache
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("junk")
    assert await changed_files(tmp_project) == ["calc.py"]


async def test_caches_in_a_snapshot_are_skipped_too(tmp_project: Path) -> None:
    """A live run listed __pycache__ because the step's snapshot already contained it."""
    from support import git

    cache = tmp_project / "__pycache__" / "calc.cpython-312.pyc"
    cache.parent.mkdir()
    cache.write_text("old")
    (tmp_project / "calc.py").write_text("x = 1\n")
    git(tmp_project, "add", "-A")
    git(tmp_project, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "snapshot")
    base = git(tmp_project, "rev-parse", "HEAD").strip()
    cache.write_text("new")
    (tmp_project / "calc.py").write_text("x = 2\n")
    assert await changed_files(tmp_project, base) == ["calc.py"]


async def test_the_review_diff_leaves_caches_out(tmp_project: Path) -> None:
    from forge.runtime.gitops import diff_since
    from support import git

    cache = tmp_project / "__pycache__" / "calc.cpython-312.pyc"
    cache.parent.mkdir()
    cache.write_text("old")
    git(tmp_project, "add", "-A")
    git(tmp_project, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "snapshot")
    cache.write_text("new")
    (tmp_project / "calc.py").write_text("x = 2\n")
    diff = await diff_since(tmp_project)
    assert "calc.py" in diff and "__pycache__" not in diff
