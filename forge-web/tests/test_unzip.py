"""Unpacking ZIP archives into the workspace: nothing leaves it, no links or devices, no bombs."""

import io
import stat
import sys
import zipfile
from pathlib import Path

import pytest

from forge_sandbox.fsops import Workspace
from forge_sandbox.rpc import RpcError
from forge_sandbox.unzip import UnzipLimits, unzip
from forge_sandbox.usage import disk_usage

POSIX_ONLY = pytest.mark.skipif(sys.platform == "win32", reason="checks POSIX file modes")


@pytest.fixture
def ws(tmp_path: Path) -> Workspace:
    root = tmp_path / "workspace"
    root.mkdir()
    return Workspace(root)


def archive(ws: Workspace, entries: list[tuple[zipfile.ZipInfo | str, bytes]]) -> str:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in entries:
            zf.writestr(name, data)
    (ws.root / "upload.zip").write_bytes(buffer.getvalue())
    return "upload.zip"


def special(name: str, mode: int) -> zipfile.ZipInfo:
    info = zipfile.ZipInfo(name)
    info.external_attr = (mode | 0o644) << 16
    return info


def code_of(err: pytest.ExceptionInfo[RpcError]) -> str:
    return err.value.code


def test_files_and_folders_are_unpacked(ws: Workspace) -> None:
    name = archive(ws, [("app/src/main.py", b"print(1)\n"), ("app/README.md", b"# hi\n"),
                        ("app/empty/", b"")])  # fmt: skip
    result = unzip(ws, name, "")
    assert result == {"files": 2, "bytes": 14, "root": "app"}  # one top folder is dropped
    assert (ws.root / "src" / "main.py").read_text() == "print(1)\n"
    assert (ws.root / "empty").is_dir()
    kept = unzip(ws, archive(ws, [("a/x.txt", b"x"), ("b.txt", b"b")]), "sub", strip_root=False)
    assert kept["root"] == "" and (ws.root / "sub" / "a" / "x.txt").exists()


@pytest.mark.parametrize(
    "bad", ["../evil.txt", "a/../../evil.txt", "/etc/evil", "C:/evil", "..\\evil"]
)
def test_entries_that_leave_are_refused(ws: Workspace, bad: str) -> None:
    name = archive(ws, [("ok.txt", b"fine"), (bad, b"pwned")])
    with pytest.raises(RpcError) as err:
        unzip(ws, name, "")
    assert code_of(err) == "unsafe_archive"
    assert not (ws.root / "ok.txt").exists()  # nothing is written when one entry is bad
    assert not (ws.root.parent / "evil.txt").exists()


@POSIX_ONLY
@pytest.mark.parametrize("mode", [stat.S_IFLNK, stat.S_IFCHR, stat.S_IFBLK, stat.S_IFIFO])
def test_links_and_devices_are_refused(ws: Workspace, mode: int) -> None:
    name = archive(ws, [(special("link", mode), b"/etc/passwd")])
    with pytest.raises(RpcError) as err:
        unzip(ws, name, "")
    assert code_of(err) == "unsafe_archive"
    assert not (ws.root / "link").exists()


def test_a_zip_bomb_stops_early(ws: Workspace) -> None:
    name = archive(ws, [("zeros.bin", b"\0" * 50_000_000)])  # compresses about 1000:1
    assert (ws.root / name).stat().st_size < 200_000
    with pytest.raises(RpcError) as err:
        unzip(ws, name, "", limits=UnzipLimits(max_ratio=100))
    assert code_of(err) == "too_large"
    assert sorted(p.name for p in ws.root.iterdir()) == [name]  # no partial file left
    with pytest.raises(RpcError) as total:
        unzip(ws, name, "", limits=UnzipLimits(max_total=1_000_000))
    assert code_of(total) == "too_large"


def test_too_many_entries_and_broken_archives(ws: Workspace) -> None:
    name = archive(ws, [(f"f{i}.txt", b"x") for i in range(20)])
    with pytest.raises(RpcError) as err:
        unzip(ws, name, "", limits=UnzipLimits(max_entries=10))
    assert code_of(err) == "too_large"
    (ws.root / "broken.zip").write_bytes(b"PK\x03\x04 not really a zip")
    with pytest.raises(RpcError) as broken:
        unzip(ws, "broken.zip", "")
    assert code_of(broken) == "bad_archive"


@POSIX_ONLY
def test_the_archive_itself_is_not_read_through_a_link(ws: Workspace, tmp_path: Path) -> None:
    outside = tmp_path / "outside.zip"
    with zipfile.ZipFile(outside, "w") as zf:
        zf.writestr("x.txt", b"x")
    (ws.root / "link.zip").symlink_to(outside)
    with pytest.raises(RpcError) as err:
        unzip(ws, "link.zip", "")
    assert code_of(err) == "is_symlink"


def test_disk_usage_counts_files_not_links(ws: Workspace, tmp_path: Path) -> None:
    (ws.root / "a").mkdir()
    (ws.root / "a" / "one.bin").write_bytes(b"x" * 1000)
    (ws.root / "two.bin").write_bytes(b"y" * 500)
    if sys.platform != "win32":
        big = tmp_path / "big.bin"
        big.write_bytes(b"z" * 100_000)
        (ws.root / "link.bin").symlink_to(big)
    assert disk_usage(ws) == {"bytes": 1500, "files": 2, "complete": True}
