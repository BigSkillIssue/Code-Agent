"""Tests for loading FORGE.md / AGENTS.md / CLAUDE.md memory files."""

import os
from pathlib import Path

import pytest

from forge.memory import load_memory, render_memory


@pytest.fixture(autouse=True)
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "home"
    path.mkdir()
    monkeypatch.setenv("FORGE_HOME", str(path))
    return path


def put(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def test_nested_files_load_general_first(tmp_path: Path, home: Path) -> None:
    root = tmp_path / "repo"
    put(home / "FORGE.md", "user rule")
    put(root / "FORGE.md", "root forge")
    put(root / "CLAUDE.md", "root claude")
    put(root / "pkg" / "AGENTS.md", "pkg agents")
    put(root / "pkg" / "sub" / "FORGE.md", "deepest")
    put(root / "other" / "FORGE.md", "not on the way down")
    files = load_memory(root, root / "pkg" / "sub")
    assert [f.text for f in files] == [
        "user rule",
        "root forge",
        "root claude",
        "pkg agents",
        "deepest",
    ]
    assert [f.scope for f in files] == ["user", ".", ".", "pkg", "pkg/sub"]


def test_missing_files_are_ignored(tmp_path: Path) -> None:
    assert load_memory(tmp_path, tmp_path) == []
    assert render_memory([], tmp_path) == "(none)"


def test_large_file_is_capped(tmp_path: Path) -> None:
    put(tmp_path / "FORGE.md", "x" * 40_000)
    (memory,) = load_memory(tmp_path, tmp_path)
    assert memory.text.startswith("x" * 32 * 1024)
    assert "[truncated: FORGE.md is 40000 bytes" in memory.text


def test_render_tags_path_and_scope(tmp_path: Path) -> None:
    put(tmp_path / "AGENTS.md", "Run `make test`.")
    text = render_memory(load_memory(tmp_path, tmp_path), tmp_path)
    assert text == '<memory path="AGENTS.md" scope=".">\nRun `make test`.\n</memory>'


def test_cwd_outside_root_uses_root_only(tmp_path: Path) -> None:
    put(tmp_path / "repo" / "FORGE.md", "root")
    assert [f.text for f in load_memory(tmp_path / "repo", Path(os.sep))] == ["root"]
