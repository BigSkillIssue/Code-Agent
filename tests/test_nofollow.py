"""Tests for reading and writing below a trusted folder without following links."""

import os
import sys
from pathlib import Path

import pytest

from forge.runtime.nofollow import (
    LinkError,
    open_below,
    read_below,
    symlink_below,
    unlink_below,
    write_below,
)

can_symlink = pytest.mark.skipif(
    sys.platform == "win32", reason="symlinks need admin rights on Windows"
)
posix_only = pytest.mark.skipif(sys.platform == "win32", reason="POSIX file modes")


def test_write_makes_folders_and_replaces_atomically(tmp_path: Path) -> None:
    target = tmp_path / "a" / "b" / "c.txt"
    write_below(tmp_path, target, b"one")
    write_below(tmp_path, target, b"two")
    assert target.read_bytes() == b"two"
    assert sorted(p.name for p in target.parent.iterdir()) == ["c.txt"]  # no temp file left


def test_append_and_read(tmp_path: Path) -> None:
    target = tmp_path / "log" / "x.log"
    write_below(tmp_path, target, b"a\n", append=True)
    write_below(tmp_path, target, b"b\n", append=True)
    assert read_below(tmp_path, target) == b"a\nb\n"
    with pytest.raises(FileNotFoundError):
        read_below(tmp_path, tmp_path / "missing.txt")


def test_paths_outside_the_base_are_refused(tmp_path: Path) -> None:
    (tmp_path / "base").mkdir()
    with pytest.raises(LinkError):
        write_below(tmp_path / "base", tmp_path / "elsewhere.txt", b"x")
    with pytest.raises(LinkError):
        write_below(tmp_path / "base", tmp_path / "base" / ".." / "elsewhere.txt", b"x")


@posix_only
def test_a_rewrite_keeps_the_mode_and_can_set_the_execute_bits(tmp_path: Path) -> None:
    script = tmp_path / "run.sh"
    script.write_text("#!/bin/sh\n")
    script.chmod(0o750)
    write_below(tmp_path, script, b"#!/bin/sh\necho hi\n")
    assert script.stat().st_mode & 0o777 == 0o750
    write_below(tmp_path, script, b"x", executable=False)
    assert script.stat().st_mode & 0o777 == 0o640
    write_below(tmp_path, tmp_path / "new.sh", b"x", executable=True)
    assert os.access(tmp_path / "new.sh", os.X_OK)


@can_symlink
def test_a_linked_folder_or_file_is_never_followed(tmp_path: Path) -> None:
    base, outside = tmp_path / "base", tmp_path / "outside"
    base.mkdir()
    outside.mkdir()
    (outside / "secret.txt").write_text("secret")
    os.symlink(outside, base / "dir", target_is_directory=True)
    os.symlink(outside / "secret.txt", base / "file.txt")
    for path in (base / "dir" / "new.txt", base / "file.txt"):
        with pytest.raises(LinkError):
            write_below(base, path, b"pwned", append=path.name == "file.txt")
    with pytest.raises(LinkError):
        read_below(base, base / "file.txt")
    with pytest.raises(LinkError):
        open_below(base, base / "dir" / "x.log", os.O_WRONLY | os.O_CREAT)
    assert sorted(p.name for p in outside.iterdir()) == ["secret.txt"]
    assert (outside / "secret.txt").read_text() == "secret"


@can_symlink
def test_unlink_removes_a_link_not_its_target(tmp_path: Path) -> None:
    (tmp_path / "target.txt").write_text("keep")
    os.symlink(tmp_path / "target.txt", tmp_path / "link.txt")
    unlink_below(tmp_path, tmp_path / "link.txt")
    unlink_below(tmp_path, tmp_path / "missing.txt")
    assert not (tmp_path / "link.txt").is_symlink()
    assert (tmp_path / "target.txt").read_text() == "keep"


@can_symlink
def test_symlink_below_replaces_a_file(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_text("a")
    write_below(tmp_path, tmp_path / "docs" / "link", b"old file")
    symlink_below(tmp_path, tmp_path / "docs" / "link", "../a.txt")
    assert os.readlink(tmp_path / "docs" / "link") == "../a.txt"
    assert (tmp_path / "docs" / "link").read_text() == "a"
