"""File operations confined to the workspace.

Paths are relative to the workspace root; `..`, absolute paths and NUL bytes are refused. On POSIX
every path is walked one component at a time with `O_NOFOLLOW` relative to the parent's directory
descriptor, so a symbolic link (even one swapped in while we work) never leads outside: the daemon
refuses to follow symbolic links at all. Windows (local mode only) falls back to a resolved-path
check.
"""

import base64
import contextlib
import errno
import os
import secrets
import shutil
import stat
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, BinaryIO

from forge_sandbox.rpc import RpcError

READ_LIMIT = 900_000  # bytes of content per read (a message carries at most 1 MiB)
WRITE_LIMIT = 900_000
UPLOAD_LIMIT = 2 * 1024**3  # bytes of one file written in parts
SNIFF = 8192
FD_SAFE = (
    all(
        f in os.supports_dir_fd
        for f in (os.open, os.mkdir, os.rename, os.unlink, os.rmdir, os.stat)
    )
    and os.scandir in os.supports_fd
)
NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
DIRECTORY = getattr(os, "O_DIRECTORY", 0)
NONBLOCK = getattr(os, "O_NONBLOCK", 0)


@dataclass(frozen=True)
class Owner:
    """The user that owns the workspace files (the daemon may run as root in a container)."""

    uid: int
    gid: int


def fs_error(code: str, message: str) -> RpcError:
    """An expected file-operation failure."""
    return RpcError(code, message)


def split_path(rel: str) -> list[str]:
    """The components of a workspace-relative path; refuses anything that could leave it."""
    if not isinstance(rel, str) or "\0" in rel or len(rel) > 4096:
        raise fs_error("invalid_path", "invalid path")
    text = rel.replace("\\", "/")
    if text.startswith("/") or (len(text) > 1 and text[1] == ":"):
        raise fs_error("invalid_path", f"{rel!r} is not relative to the workspace")
    parts = [p for p in text.split("/") if p not in ("", ".")]
    if any(p == ".." for p in parts):
        raise fs_error("invalid_path", f"{rel!r} leaves the workspace")
    if any(len(p) > 255 for p in parts):
        raise fs_error("invalid_path", "a path component is too long")
    return parts


def entry_type(mode: int) -> str:
    """file, dir, symlink or other."""
    if stat.S_ISLNK(mode):
        return "symlink"
    if stat.S_ISDIR(mode):
        return "dir"
    return "file" if stat.S_ISREG(mode) else "other"


def stat_info(name: str, st: os.stat_result) -> dict[str, Any]:
    """What a listing or stat call reports about one entry."""
    return {"name": name, "type": entry_type(st.st_mode), "size": st.st_size, "mtime": st.st_mtime}


def decode_content(data: bytes) -> dict[str, Any]:
    """Text if the bytes look like UTF-8 text, else base64."""
    if b"\0" not in data[:SNIFF]:
        with contextlib.suppress(UnicodeDecodeError):
            return {"binary": False, "text": data.decode("utf-8"), "base64": None}
    return {"binary": True, "text": None, "base64": base64.b64encode(data).decode("ascii")}


class Workspace:
    """One project's files (all methods block: call them through asyncio.to_thread)."""

    def __init__(self, root: Path, owner: Owner | None = None) -> None:
        self.root = root.resolve()
        self.owner = owner

    # descriptors ----------------------------------------------------------------------------

    def _open_dir(self, parts: list[str], *, create: bool = False) -> int:
        """A descriptor of the directory at `parts`, walked without following links."""
        fd = os.open(self.root, os.O_RDONLY | DIRECTORY)
        try:
            for part in parts:
                try:
                    child = os.open(part, os.O_RDONLY | DIRECTORY | NOFOLLOW, dir_fd=fd)
                except FileNotFoundError:
                    if not create:
                        raise fs_error("not_found", f"{'/'.join(parts)} does not exist") from None
                    os.mkdir(part, 0o755, dir_fd=fd)
                    self._chown(part, fd)
                    child = os.open(part, os.O_RDONLY | DIRECTORY | NOFOLLOW, dir_fd=fd)
                except OSError as err:
                    raise self._walk_error(err, parts) from None
                os.close(fd)
                fd = child
            return fd
        except BaseException:
            os.close(fd)
            raise

    def _walk_error(self, err: OSError, parts: list[str]) -> RpcError:
        path = "/".join(parts)
        if err.errno == errno.ELOOP:
            return fs_error("is_symlink", f"{path} goes through a symbolic link")
        if err.errno == errno.ENOTDIR:
            return fs_error("not_a_directory", f"{path} is not a directory")
        return fs_error("io_error", f"{path}: {err.strerror}")

    def _parent(self, rel: str, *, create: bool = False) -> tuple[int, str, list[str]]:
        parts = split_path(rel)
        if not parts:
            raise fs_error("invalid_path", "the workspace root itself cannot be used here")
        return self._open_dir(parts[:-1], create=create), parts[-1], parts

    def _as_root(self) -> bool:
        return self.owner is not None and sys.platform != "win32" and os.geteuid() == 0

    def _chown(self, name: str, dir_fd: int) -> None:
        if self.owner is not None and self._as_root():
            os.chown(name, self.owner.uid, self.owner.gid, dir_fd=dir_fd, follow_symlinks=False)

    def _claim(self, fd: int, current: os.stat_result | None) -> None:
        """Give a new file the old file's mode and the workspace owner, through its descriptor
        (a name could be swapped for a link meanwhile)."""
        if sys.platform == "win32":
            return
        if current is not None:
            os.fchmod(fd, stat.S_IMODE(current.st_mode))
        if self.owner is not None and self._as_root():
            os.fchown(fd, self.owner.uid, self.owner.gid)

    def _lstat(self, name: str, dir_fd: int) -> os.stat_result | None:
        try:
            return os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
        except FileNotFoundError:
            return None

    # operations -----------------------------------------------------------------------------

    def list(self, rel: str = "") -> dict[str, Any]:
        """The entries of a directory, directories first, then by name."""
        if not FD_SAFE:
            return self._fallback_list(rel)
        fd = self._open_dir(split_path(rel))
        try:
            entries = [stat_info(e.name, e.stat(follow_symlinks=False)) for e in os.scandir(fd)]
        finally:
            os.close(fd)
        entries.sort(key=lambda e: (e["type"] != "dir", e["name"].lower()))
        return {"path": "/".join(split_path(rel)), "entries": entries}

    def stat(self, rel: str) -> dict[str, Any]:
        """Type, size and modification time of one entry."""
        if not FD_SAFE:
            return stat_info(Path(rel).name, self._fallback_path(rel).lstat())
        dir_fd, name, _ = self._parent(rel)
        try:
            st = self._lstat(name, dir_fd)
        finally:
            os.close(dir_fd)
        if st is None:
            raise fs_error("not_found", f"{rel} does not exist")
        return stat_info(name, st)

    def read(self, rel: str, limit: int = READ_LIMIT, offset: int = 0) -> dict[str, Any]:
        """A regular file's content (text or base64): at most `limit` bytes from `offset`."""
        limit = max(0, min(limit, READ_LIMIT))
        if not FD_SAFE:
            return self._fallback_read(rel, limit, offset)
        dir_fd, name, _ = self._parent(rel)
        try:
            fd = self._open_file(name, dir_fd, os.O_RDONLY)
        finally:
            os.close(dir_fd)
        with os.fdopen(fd, "rb") as handle:
            st = os.fstat(handle.fileno())
            handle.seek(offset)
            data = handle.read(limit + 1)
        return self._content(rel, st, data, limit, offset)

    def open_read(self, rel: str) -> BinaryIO:
        """A regular file opened for reading, found without following links."""
        if not FD_SAFE:
            path = self._fallback_path(rel)
            if not path.is_file() or path.is_symlink():
                raise fs_error("not_found", f"{rel} is not a file")
            return path.open("rb")
        dir_fd, name, _ = self._parent(rel)
        try:
            fd = self._open_file(name, dir_fd, os.O_RDONLY)
        finally:
            os.close(dir_fd)
        return os.fdopen(fd, "rb")

    def _open_file(self, name: str, dir_fd: int, flags: int) -> int:
        try:
            # O_NONBLOCK: opening a FIFO must not hang; the type check below refuses it.
            fd = os.open(name, flags | NOFOLLOW | NONBLOCK, dir_fd=dir_fd)
        except FileNotFoundError:
            raise fs_error("not_found", f"{name} does not exist") from None
        except OSError as err:
            if err.errno == errno.ELOOP:
                raise fs_error("is_symlink", f"{name} is a symbolic link") from None
            if err.errno == errno.EISDIR:
                raise fs_error("not_a_file", f"{name} is a directory") from None
            raise fs_error("io_error", f"{name}: {err.strerror}") from None
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            os.close(fd)
            raise fs_error("not_a_file", f"{name} is not a regular file")
        os.set_blocking(fd, True)
        return fd

    def _content(
        self, rel: str, st: os.stat_result, data: bytes, limit: int, offset: int = 0
    ) -> dict[str, Any]:
        truncated = len(data) > limit
        # A later part may start inside a UTF-8 character: those always travel as base64.
        body = decode_content(data[:limit]) if offset == 0 else {
            "binary": True, "text": None, "base64": base64.b64encode(data[:limit]).decode(),
        }  # fmt: skip
        path = "/".join(split_path(rel))
        return {
            "path": path,
            "size": st.st_size,
            "mtime": st.st_mtime,
            "offset": offset,
            "truncated": truncated,
            **body,
        }

    def write(
        self,
        rel: str,
        data: bytes,
        *,
        create_dirs: bool = False,
        expected_mtime: float | None = None,
    ) -> dict[str, Any]:
        """Replace (or create) a regular file atomically; `expected_mtime` guards edits."""
        if len(data) > WRITE_LIMIT:
            raise fs_error("too_large", f"at most {WRITE_LIMIT} bytes per write")
        if not FD_SAFE:
            return self._fallback_write(rel, data, create_dirs, expected_mtime)
        dir_fd, name, parts = self._parent(rel, create=create_dirs)
        try:
            current = self._lstat(name, dir_fd)
            self._check_replaceable(rel, current, expected_mtime)
            temp = f".{name}.forge-{secrets.token_hex(4)}"
            fd = os.open(
                temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | NOFOLLOW, 0o644, dir_fd=dir_fd
            )
            try:
                with os.fdopen(fd, "wb") as handle:
                    self._claim(handle.fileno(), current)
                    handle.write(data)
                os.rename(temp, name, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
            except BaseException:
                with contextlib.suppress(OSError):
                    os.unlink(temp, dir_fd=dir_fd)
                raise
            st = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
        finally:
            os.close(dir_fd)
        return {"path": "/".join(parts), "size": st.st_size, "mtime": st.st_mtime}

    def write_part(
        self,
        rel: str,
        upload: str,
        data: bytes,
        *,
        last: bool,
        create_dirs: bool = False,
        abort: bool = False,
    ) -> dict[str, Any]:
        """Write a large file in parts: each part is appended to a hidden file next to it, the
        last one puts that file in place at once; `abort` throws the parts away."""
        if len(data) > WRITE_LIMIT:
            raise fs_error("too_large", f"at most {WRITE_LIMIT} bytes per part")
        if not FD_SAFE:
            return self._fallback_write_part(rel, upload, data, last, create_dirs, abort)
        dir_fd, name, parts = self._parent(rel, create=create_dirs)
        temp, path = f".{name}.upload-{upload}", "/".join(parts)
        try:
            if abort:
                with contextlib.suppress(FileNotFoundError):
                    os.unlink(temp, dir_fd=dir_fd)
                return {"path": path, "aborted": True}
            size = self._append(temp, name, dir_fd, data)
            if not last:
                return {"path": path, "received": size, "done": False}
            self._check_replaceable(rel, self._lstat(name, dir_fd), None)
            os.rename(temp, name, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
            st = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
        finally:
            os.close(dir_fd)
        return {"path": path, "size": st.st_size, "mtime": st.st_mtime, "done": True}

    def _append(self, temp: str, name: str, dir_fd: int, data: bytes) -> int:
        """Add `data` to the upload's hidden file (created by the first part); its new size."""
        try:
            fd = self._open_file(temp, dir_fd, os.O_WRONLY | os.O_APPEND)
            new = False
        except RpcError as err:
            if err.code != "not_found":
                raise
            fd = os.open(
                temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | NOFOLLOW, 0o644, dir_fd=dir_fd
            )
            new = True
        with os.fdopen(fd, "ab") as handle:
            if new:
                self._claim(handle.fileno(), self._lstat(name, dir_fd))
            handle.write(data)
            size = handle.tell()
        if size > UPLOAD_LIMIT:
            os.unlink(temp, dir_fd=dir_fd)
            raise fs_error("too_large", f"a file may have at most {UPLOAD_LIMIT} bytes")
        return size

    def _check_replaceable(
        self, rel: str, current: os.stat_result | None, expected_mtime: float | None
    ) -> None:
        if current is not None and not stat.S_ISREG(current.st_mode):
            kind = entry_type(current.st_mode)
            raise fs_error(
                "is_symlink" if kind == "symlink" else "not_a_file", f"{rel} is a {kind}"
            )
        changed = current is None or abs(current.st_mtime - (expected_mtime or 0)) > 1e-6
        if expected_mtime is not None and changed:
            raise fs_error("conflict", f"{rel} changed since it was read")

    def mkdir(self, rel: str) -> dict[str, Any]:
        """Create a directory (and missing parents)."""
        if not FD_SAFE:
            self._fallback_path(rel).mkdir(parents=True, exist_ok=True)
            return {"path": rel}
        os.close(self._open_dir(split_path(rel), create=True))
        return {"path": "/".join(split_path(rel))}

    def rename(self, src: str, dst: str) -> dict[str, Any]:
        """Move an entry inside the workspace; refuses to replace an existing one."""
        if not FD_SAFE:
            target = self._fallback_path(dst)
            if target.exists():
                raise fs_error("exists", f"{dst} already exists")
            self._fallback_path(src).rename(target)
            return {"path": dst}
        src_fd, src_name, _ = self._parent(src)
        try:
            dst_fd, dst_name, dst_parts = self._parent(dst, create=True)
            try:
                if self._lstat(src_name, src_fd) is None:
                    raise fs_error("not_found", f"{src} does not exist")
                if self._lstat(dst_name, dst_fd) is not None:
                    raise fs_error("exists", f"{dst} already exists")
                os.rename(src_name, dst_name, src_dir_fd=src_fd, dst_dir_fd=dst_fd)
            finally:
                os.close(dst_fd)
        finally:
            os.close(src_fd)
        return {"path": "/".join(dst_parts)}

    def delete(self, rel: str, *, recursive: bool = False) -> dict[str, Any]:
        """Remove a file, a link (not its target) or a directory (`recursive` for non-empty)."""
        if not FD_SAFE:
            return self._fallback_delete(rel, recursive)
        dir_fd, name, parts = self._parent(rel)
        try:
            st = self._lstat(name, dir_fd)
            if st is None:
                raise fs_error("not_found", f"{rel} does not exist")
            if stat.S_ISDIR(st.st_mode):
                if recursive:
                    shutil.rmtree(name, dir_fd=dir_fd)  # fd-based: never follows links
                else:
                    try:
                        os.rmdir(name, dir_fd=dir_fd)
                    except OSError:
                        raise fs_error("not_empty", f"{rel} is not empty") from None
            else:
                os.unlink(name, dir_fd=dir_fd)
        finally:
            os.close(dir_fd)
        return {"path": "/".join(parts)}

    # Windows fallback (local mode only) ---------------------------------------------------------

    def _fallback_path(self, rel: str) -> Path:
        path = self.root.joinpath(*split_path(rel))
        resolved = path.resolve()
        if resolved != self.root and self.root not in resolved.parents:
            raise fs_error("is_symlink", f"{rel} leads outside the workspace")
        return path

    def _fallback_list(self, rel: str) -> dict[str, Any]:
        base = self._fallback_path(rel)
        if not base.is_dir():
            raise fs_error("not_found", f"{rel or '.'} is not a directory")
        entries = [stat_info(p.name, p.lstat()) for p in base.iterdir()]
        entries.sort(key=lambda e: (e["type"] != "dir", e["name"].lower()))
        return {"path": "/".join(split_path(rel)), "entries": entries}

    def _fallback_read(self, rel: str, limit: int, offset: int) -> dict[str, Any]:
        path = self._fallback_path(rel)
        if not path.is_file() or path.is_symlink():
            raise fs_error("not_found", f"{rel} is not a file")
        with path.open("rb") as handle:
            handle.seek(offset)
            data = handle.read(limit + 1)
        return self._content(rel, path.stat(), data, limit, offset)

    def _fallback_write_part(
        self, rel: str, upload: str, data: bytes, last: bool, create_dirs: bool, abort: bool
    ) -> dict[str, Any]:
        path = self._fallback_path(rel)
        temp = path.with_name(f".{path.name}.upload-{upload}")
        rel_path = "/".join(split_path(rel))
        if abort:
            temp.unlink(missing_ok=True)
            return {"path": rel_path, "aborted": True}
        if create_dirs:
            path.parent.mkdir(parents=True, exist_ok=True)
        with temp.open("ab") as handle:
            handle.write(data)
            size = handle.tell()
        if not last:
            return {"path": rel_path, "received": size, "done": False}
        os.replace(temp, path)
        st = path.stat()
        return {"path": rel_path, "size": st.st_size, "mtime": st.st_mtime, "done": True}

    def _fallback_write(
        self, rel: str, data: bytes, create_dirs: bool, expected_mtime: float | None
    ) -> dict[str, Any]:
        path = self._fallback_path(rel)
        if create_dirs:
            path.parent.mkdir(parents=True, exist_ok=True)
        current = path.lstat() if path.exists() or path.is_symlink() else None
        self._check_replaceable(rel, current, expected_mtime)
        temp = path.with_name(f".{path.name}.forge-{secrets.token_hex(4)}")
        temp.write_bytes(data)
        os.replace(temp, path)
        st = path.stat()
        return {"path": "/".join(split_path(rel)), "size": st.st_size, "mtime": st.st_mtime}

    def _fallback_delete(self, rel: str, recursive: bool) -> dict[str, Any]:
        path = self._fallback_path(rel)
        if path.is_dir() and not path.is_symlink():
            if recursive:
                shutil.rmtree(path)
            else:
                path.rmdir()
        elif path.exists() or path.is_symlink():
            path.unlink()
        else:
            raise fs_error("not_found", f"{rel} does not exist")
        return {"path": "/".join(split_path(rel))}
