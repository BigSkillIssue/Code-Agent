"""Unpacking a ZIP archive into the workspace, safely.

Every entry is checked before anything is written: no entry may leave the destination (no `..`,
no absolute or drive paths), and links, devices, pipes and sockets are refused. While unpacking,
the bytes actually produced are counted (not what the archive claims), so a zip bomb stops early,
and each file appears only when it is complete.
"""

import contextlib
import secrets
import stat
import zipfile
from dataclasses import dataclass
from typing import Any

from forge_sandbox.fsops import Workspace, fs_error, split_path
from forge_sandbox.rpc import RpcError

PART = 512 * 1024
SMALL = 1024 * 1024  # files below this size are never treated as bombs


@dataclass(frozen=True)
class UnzipLimits:
    """How much one archive may unpack."""

    max_entries: int = 50_000
    max_total: int = 4 * 1024**3  # bytes unpacked
    max_ratio: int = 200  # unpacked bytes per compressed byte, for a file above SMALL


def entry_kind(info: zipfile.ZipInfo) -> str:
    """dir, file or the kind of special entry it is (from the Unix mode, when there is one)."""
    mode = info.external_attr >> 16
    if stat.S_ISLNK(mode):
        return "link"
    if stat.S_ISCHR(mode) or stat.S_ISBLK(mode) or stat.S_ISFIFO(mode) or stat.S_ISSOCK(mode):
        return "device"
    return "dir" if info.is_dir() else "file"


def entry_parts(name: str) -> list[str]:
    """The path parts of an entry; refuses names that would leave the destination."""
    raw = name.replace("\\", "/")
    if raw.startswith("/") or (len(raw) > 1 and raw[1] == ":") or "\0" in raw:
        raise fs_error("unsafe_archive", f"the archive entry {name!r} is not a relative path")
    parts = [p for p in raw.split("/") if p not in ("", ".")]
    if ".." in parts:
        raise fs_error("unsafe_archive", f"the archive entry {name!r} leaves its folder")
    return parts


def common_root(infos: list[zipfile.ZipInfo]) -> str:
    """The one top folder every entry is in (as in GitHub downloads), or ""."""
    tops = {tuple(entry_parts(i.filename)[:1]) for i in infos}
    nested = any(len(entry_parts(i.filename)) > 1 for i in infos)
    if len(tops) == 1 and nested:
        (top,) = tops
        return top[0] if top else ""
    return ""


def check_entries(infos: list[zipfile.ZipInfo], limits: UnzipLimits) -> None:
    """Refuse the whole archive if any entry is unsafe."""
    if len(infos) > limits.max_entries:
        raise fs_error("too_large", f"the archive has more than {limits.max_entries} entries")
    for info in infos:
        kind = entry_kind(info)
        if kind in ("link", "device"):
            raise fs_error("unsafe_archive", f"the archive entry {info.filename!r} is a {kind}")
        if info.flag_bits & 0x1:
            raise fs_error("bad_archive", "encrypted archives cannot be unpacked")
        entry_parts(info.filename)


def unzip(
    ws: Workspace,
    archive: str,
    dest: str = "",
    *,
    strip_root: bool = True,
    limits: UnzipLimits | None = None,
) -> dict[str, Any]:
    """Unpack `archive` (a workspace path) into `dest`; counts of files and bytes."""
    limits = limits or UnzipLimits()
    with ws.open_read(archive) as handle:
        try:
            with zipfile.ZipFile(handle) as zf:
                infos = zf.infolist()
                check_entries(infos, limits)
                root = common_root(infos) if strip_root else ""
                return unpack(ws, zf, infos, dest, root, limits)
        except (zipfile.BadZipFile, zipfile.LargeZipFile, EOFError, NotImplementedError) as err:
            raise fs_error("bad_archive", f"the archive cannot be read: {err}") from None


def unpack(
    ws: Workspace,
    zf: zipfile.ZipFile,
    infos: list[zipfile.ZipInfo],
    dest: str,
    root: str,
    limits: UnzipLimits,
) -> dict[str, Any]:
    """Write the checked entries."""
    base = split_path(dest)
    files = total = 0
    for info in infos:
        parts = entry_parts(info.filename)
        if root and parts[:1] == [root]:
            parts = parts[1:]
        if not parts:
            continue
        path = "/".join([*base, *parts])
        if entry_kind(info) == "dir":
            ws.mkdir(path)
            continue
        total = write_entry(ws, zf, info, path, total, limits)
        files += 1
    return {"files": files, "bytes": total, "root": root}


def write_entry(
    ws: Workspace,
    zf: zipfile.ZipFile,
    info: zipfile.ZipInfo,
    path: str,
    total: int,
    limits: UnzipLimits,
) -> int:
    """Unpack one file part by part; the running total of unpacked bytes."""
    upload, written = secrets.token_hex(8), 0
    try:
        with zf.open(info) as source:
            while chunk := source.read(PART):
                written += len(chunk)
                total += len(chunk)
                if total > limits.max_total:
                    raise fs_error(
                        "too_large", f"the archive unpacks to more than {limits.max_total} bytes"
                    )
                if written > SMALL and written > limits.max_ratio * max(info.compress_size, 1):
                    raise fs_error(
                        "too_large", f"{info.filename} unpacks far larger than it is packed"
                    )
                ws.write_part(path, upload, chunk, last=False, create_dirs=True)
        ws.write_part(path, upload, b"", last=True, create_dirs=True)
    except BaseException:
        with contextlib.suppress(RpcError):
            ws.write_part(path, upload, b"", last=False, create_dirs=True, abort=True)
        raise
    return total
