"""Workspace file operations stay inside the workspace, whatever the path or link says."""

import os
import sys
from pathlib import Path

import pytest

from forge_sandbox import fsops
from forge_sandbox.fsops import FD_SAFE, Workspace, split_path
from forge_sandbox.rpc import RpcError

POSIX_ONLY = pytest.mark.skipif(
    sys.platform == "win32", reason="symlink and FIFO checks need POSIX"
)


@pytest.fixture
def ws(tmp_path: Path) -> Workspace:
    root = tmp_path / "workspace"
    root.mkdir()
    return Workspace(root)


@pytest.fixture
def outside(tmp_path: Path) -> Path:
    folder = tmp_path / "outside"
    folder.mkdir()
    (folder / "secret.txt").write_text("top secret")
    return folder


def code_of(error: pytest.ExceptionInfo[RpcError]) -> str:
    return error.value.code


@pytest.mark.parametrize("bad", ["../x", "a/../../x", "/etc/passwd", "C:/Windows", "a\0b", "..\\x"])
def test_paths_that_leave_are_refused(bad: str) -> None:
    with pytest.raises(RpcError) as err:
        split_path(bad)
    assert code_of(err) == "invalid_path"


def test_split_path_normalizes() -> None:
    assert split_path("") == []
    assert split_path("./a//b/") == ["a", "b"]
    assert split_path("a\\b") == ["a", "b"]


def test_write_read_list_rename_delete(ws: Workspace) -> None:
    written = ws.write("src/app.py", b"print('hi')\r\n", create_dirs=True)
    assert written["path"] == "src/app.py" and written["size"] == 13
    read = ws.read("src/app.py")
    assert read["text"] == "print('hi')\r\n" and not read["binary"] and not read["truncated"]
    listing = ws.list("")
    assert [(e["name"], e["type"]) for e in listing["entries"]] == [("src", "dir")]
    ws.rename("src/app.py", "lib/main.py")
    assert ws.stat("lib/main.py")["type"] == "file"
    with pytest.raises(RpcError) as missing:
        ws.read("src/app.py")
    assert code_of(missing) == "not_found"
    ws.delete("lib", recursive=True)
    assert [e["name"] for e in ws.list("")["entries"]] == ["src"]


def test_binary_and_truncated_reads(ws: Workspace) -> None:
    ws.write("blob.bin", b"\0\1\2\3")
    blob = ws.read("blob.bin")
    assert blob["binary"] and blob["base64"] == "AAECAw=="
    ws.write("big.txt", b"x" * 5000)
    part = ws.read("big.txt", limit=100)
    assert part["truncated"] and len(part["text"]) == 100 and part["size"] == 5000


def test_write_conflict_detection(ws: Workspace) -> None:
    first = ws.write("notes.md", b"one")
    ws.write("notes.md", b"two", expected_mtime=first["mtime"])
    with pytest.raises(RpcError) as err:
        ws.write("notes.md", b"three", expected_mtime=first["mtime"] - 100)
    assert code_of(err) == "conflict"
    assert ws.read("notes.md")["text"] == "two"


def test_rename_refuses_to_replace_and_delete_refuses_non_empty(ws: Workspace) -> None:
    ws.write("a.txt", b"a")
    ws.write("b.txt", b"b")
    with pytest.raises(RpcError) as exists:
        ws.rename("a.txt", "b.txt")
    assert code_of(exists) == "exists"
    ws.write("dir/file.txt", b"x", create_dirs=True)
    with pytest.raises(RpcError) as not_empty:
        ws.delete("dir")
    assert code_of(not_empty) == "not_empty"


def test_the_fallback_without_directory_descriptors_behaves_the_same(
    ws: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Windows has no directory descriptors: there the path-based fallback runs.
    monkeypatch.setattr(fsops, "FD_SAFE", False)
    test_write_read_list_rename_delete(ws)
    test_rename_refuses_to_replace_and_delete_refuses_non_empty(ws)
    with pytest.raises(RpcError) as missing:
        ws.rename("no-such-file", "elsewhere")
    assert code_of(missing) == "not_found"


def test_write_is_limited(ws: Workspace) -> None:
    with pytest.raises(RpcError) as err:
        ws.write("huge.bin", b"x" * 1_000_000)
    assert code_of(err) == "too_large"


@POSIX_ONLY
def test_symlinked_directory_is_not_followed(ws: Workspace, outside: Path) -> None:
    (ws.root / "escape").symlink_to(outside, target_is_directory=True)
    for attempt in (
        lambda: ws.read("escape/secret.txt"),
        lambda: ws.list("escape"),
        lambda: ws.write("escape/new.txt", b"pwned"),
    ):
        with pytest.raises(RpcError) as err:
            attempt()
        assert code_of(err) in ("is_symlink", "not_a_directory")
    assert not (outside / "new.txt").exists()


@POSIX_ONLY
def test_symlinked_file_is_neither_read_nor_written_through(ws: Workspace, outside: Path) -> None:
    (ws.root / "link.txt").symlink_to(outside / "secret.txt")
    with pytest.raises(RpcError) as read:
        ws.read("link.txt")
    assert code_of(read) == "is_symlink"
    with pytest.raises(RpcError) as write:
        ws.write("link.txt", b"overwritten")
    assert code_of(write) == "is_symlink"
    assert (outside / "secret.txt").read_text() == "top secret"
    assert ws.stat("link.txt")["type"] == "symlink"


@POSIX_ONLY
def test_recursive_delete_removes_links_not_targets(ws: Workspace, outside: Path) -> None:
    ws.mkdir("dir/inner")
    (ws.root / "dir" / "inner" / "to-outside").symlink_to(outside, target_is_directory=True)
    ws.delete("dir", recursive=True)
    assert (outside / "secret.txt").read_text() == "top secret"
    assert not (ws.root / "dir").exists()


@POSIX_ONLY
def test_fifo_is_refused_without_hanging(ws: Workspace) -> None:
    os.mkfifo(ws.root / "pipe")
    with pytest.raises(RpcError) as err:
        ws.read("pipe")
    assert code_of(err) == "not_a_file"


@POSIX_ONLY
def test_descriptor_walk_is_available_on_posix() -> None:
    # Linux and macOS must take the race-free path, not the Windows fallback.
    assert FD_SAFE


def test_reads_continue_from_an_offset(ws: Workspace) -> None:
    ws.write("data.bin", bytes(range(256)) * 10)
    first = ws.read("data.bin", limit=1000)
    rest = ws.read("data.bin", limit=2000, offset=1000)
    assert first["truncated"] and not rest["truncated"]
    assert rest["offset"] == 1000 and rest["size"] == 2560
    import base64

    joined = base64.b64decode(first["base64"]) + base64.b64decode(rest["base64"])
    assert joined == bytes(range(256)) * 10


def test_large_files_are_written_in_parts(ws: Workspace) -> None:
    upload = "0123456789abcdef"
    for index in range(3):
        result = ws.write_part("big/file.bin", upload, b"x" * 500_000, last=False, create_dirs=True)
        assert result == {"path": "big/file.bin", "received": 500_000 * (index + 1), "done": False}
        assert not (ws.root / "big" / "file.bin").exists()  # nothing in place until the end
    done = ws.write_part("big/file.bin", upload, b"end", last=True)
    assert done["done"] and done["size"] == 1_500_003
    assert (ws.root / "big" / "file.bin").read_bytes().endswith(b"xend")
    assert [p.name for p in (ws.root / "big").iterdir()] == ["file.bin"]


def test_an_upload_can_be_abandoned(ws: Workspace) -> None:
    ws.write("keep.txt", b"old")
    ws.write_part("keep.txt", "aaaaaaaaaaaaaaaa", b"new", last=False)
    assert ws.write_part("keep.txt", "aaaaaaaaaaaaaaaa", b"", last=False, abort=True)["aborted"]
    assert sorted(p.name for p in ws.root.iterdir()) == ["keep.txt"]
    assert (ws.root / "keep.txt").read_bytes() == b"old"


@POSIX_ONLY
def test_an_upload_part_never_follows_a_link(ws: Workspace, outside: Path) -> None:
    upload = "bbbbbbbbbbbbbbbb"
    (ws.root / f".notes.txt.upload-{upload}").symlink_to(outside / "secret.txt")
    with pytest.raises(RpcError) as err:
        ws.write_part("notes.txt", upload, b"appended", last=False)
    assert code_of(err) == "is_symlink"
    assert (outside / "secret.txt").read_text() == "top secret"
