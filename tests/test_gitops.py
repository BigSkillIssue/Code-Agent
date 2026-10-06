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
