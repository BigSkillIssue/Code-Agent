"""Read and write files below a trusted folder without following a symbolic link on the way.

Forge writes its own files (job logs, spilled output, the audit log, the undo journal, caches,
reports) into `.forge/` and the changes the model asked for into the project, outside the
command sandbox. Sandboxed commands can write there too, so they could plant a link that sends
one of these writes, or a read, somewhere else. Below the trusted base every folder is opened
with O_NOFOLLOW and relative to its parent, so a planted link makes the operation fail instead.
Windows has no O_NOFOLLOW for folders: there each part is checked first (a best effort).
"""

import contextlib
import errno
import os
import stat
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

POSIX = hasattr(os, "O_NOFOLLOW") and {
    os.open,
    os.mkdir,
    os.rename,
    os.unlink,
    os.stat,
    os.symlink,
} <= set(os.supports_dir_fd)
LINK_ERRNOS = frozenset({errno.ELOOP, errno.ENOTDIR, errno.EMLINK})


class LinkError(OSError):
    """A path below the trusted folder runs through a symbolic link (or leaves the folder)."""


def write_below(
    base: Path,
    path: Path,
    data: bytes,
    *,
    append: bool = False,
    executable: bool | None = None,
) -> None:
    """Write (atomically, keeping an existing file's mode) or append to `path` inside `base`;
    `executable` True or False sets or clears the execute bits."""
    with folder_below(base, path.parent, create=True) as folder:
        if append:
            fd = _open(folder, path, os.O_WRONLY | os.O_CREAT | os.O_APPEND)
            _write_all(fd, data)
            return
        temp = path.with_name(f".{path.name}.forge-{uuid.uuid4().hex[:8]}.tmp")
        mode = _mode_of(folder, path)
        fd = _open(folder, temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL)
        with os.fdopen(fd, "wb") as file:
            if hasattr(os, "fchmod"):
                if mode is None:
                    mode = stat.S_IMODE(os.fstat(file.fileno()).st_mode)
                if executable is not None:
                    readable = mode & 0o444
                    mode = (mode | readable >> 2) if executable else (mode & ~0o111)
                os.fchmod(file.fileno(), mode)
            file.write(data)
        try:
            _replace(folder, temp, path)
        except OSError:
            _unlink(folder, temp)
            raise


def symlink_below(base: Path, path: Path, target: str) -> None:
    """Make `path` inside `base` a link to `target` (replacing a file there); where links
    cannot be made (Windows without the right), a file holding the target, as git does."""
    unlink_below(base, path)
    with folder_below(base, path.parent, create=True) as folder:
        try:
            if folder is None:
                os.symlink(target, path)
            else:
                os.symlink(target, path.name, dir_fd=folder)
        except OSError:
            if folder is not None:
                raise
    if folder is None and not path.is_symlink():
        write_below(base, path, target.encode("utf-8"))


def read_below(base: Path, path: Path) -> bytes:
    """The bytes of `path` inside `base`; FileNotFoundError when it is missing."""
    with folder_below(base, path.parent, create=False) as folder:
        fd = _open(folder, path, os.O_RDONLY)
        with os.fdopen(fd, "rb") as file:
            return file.read()


def unlink_below(base: Path, path: Path) -> None:
    """Delete `path` inside `base` if it exists (a link itself is deleted, never its target)."""
    try:
        with folder_below(base, path.parent, create=False) as folder:
            _unlink(folder, path)
    except FileNotFoundError:
        pass


def open_below(base: Path, path: Path, flags: int, mode: int = 0o666) -> int:
    """A file descriptor for `path` inside `base` (folders made when `flags` has O_CREAT)."""
    with folder_below(base, path.parent, create=bool(flags & os.O_CREAT)) as folder:
        return _open(folder, path, flags, mode)


@contextmanager
def folder_below(base: Path, folder: Path, *, create: bool) -> Iterator[int | None]:
    """A descriptor of `folder` (inside `base`) reached without following links; None on
    Windows, where each part was checked instead."""
    parts = _parts(base, folder)
    if not POSIX:
        _check_parts(base, parts, create)
        yield None
        return
    fd = os.open(base, os.O_RDONLY | os.O_DIRECTORY)
    try:
        for name in parts:
            child = _enter(fd, name, create)
            os.close(fd)
            fd = child
        yield fd
    finally:
        os.close(fd)


def _enter(parent: int, name: str, create: bool) -> int:
    """Open the sub-folder `name` of `parent`; never through a link."""
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    try:
        try:
            return os.open(name, flags, dir_fd=parent)
        except FileNotFoundError:
            if not create:
                raise
            # made at the same moment by someone else is fine: opening it checks it
            with contextlib.suppress(FileExistsError):
                os.mkdir(name, dir_fd=parent)
            return os.open(name, flags, dir_fd=parent)
    except OSError as exc:
        if exc.errno in LINK_ERRNOS:
            raise LinkError(exc.errno, f"{name} is a link or not a folder") from exc
        raise


def _parts(base: Path, folder: Path) -> tuple[str, ...]:
    """The names from `base` down to `folder`; LinkError when `folder` is outside `base`."""
    for candidate in (base, base.resolve()):
        try:
            parts = folder.relative_to(candidate).parts
        except ValueError:
            continue
        if ".." not in parts:
            return parts
    raise LinkError(errno.EXDEV, f"{folder} is outside {base}")


def _check_parts(base: Path, parts: tuple[str, ...], create: bool) -> None:
    """Windows: refuse links and junctions below `base`, making missing folders."""
    current = base
    for name in parts:
        current = current / name
        if current.is_symlink() or current.is_junction():
            raise LinkError(errno.ELOOP, f"{current} is a link")
        if not current.exists() and create:
            current.mkdir(exist_ok=True)


def _open(folder: int | None, path: Path, flags: int, mode: int = 0o666) -> int:
    """Open the last part of `path` in `folder` without following a link there either."""
    binary = getattr(os, "O_BINARY", 0)
    if folder is None:
        if path.is_symlink():
            raise LinkError(errno.ELOOP, f"{path} is a link")
        return os.open(path, flags | binary, mode)
    try:
        return os.open(path.name, flags | os.O_NOFOLLOW | binary, mode, dir_fd=folder)
    except OSError as exc:
        if exc.errno in LINK_ERRNOS:
            raise LinkError(exc.errno, f"{path} is a link") from exc
        raise


def _mode_of(folder: int | None, path: Path) -> int | None:
    """Permission bits of an existing regular file, so a rewrite keeps them (e.g. +x)."""
    try:
        if folder is None:
            info = os.lstat(path)
        else:
            info = os.stat(path.name, dir_fd=folder, follow_symlinks=False)
    except FileNotFoundError:
        return None
    return stat.S_IMODE(info.st_mode) if stat.S_ISREG(info.st_mode) else None


def _replace(folder: int | None, source: Path, target: Path) -> None:
    if folder is None:
        os.replace(source, target)
    else:
        os.replace(source.name, target.name, src_dir_fd=folder, dst_dir_fd=folder)


def _unlink(folder: int | None, path: Path) -> None:
    try:
        if folder is None:
            path.unlink()
        else:
            os.unlink(path.name, dir_fd=folder)
    except FileNotFoundError:
        pass


def _write_all(fd: int, data: bytes) -> None:
    with os.fdopen(fd, "wb") as file:
        file.write(data)
