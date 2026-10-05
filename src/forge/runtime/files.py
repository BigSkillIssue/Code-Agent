"""Paths, text decoding and the one function every file write goes through."""

import asyncio
import base64
import json
import os
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from forge.config import ForgeConfig
from forge.runtime.errors import ToolError
from forge.runtime.ledger import ReadLedger

if TYPE_CHECKING:
    from forge.ctx import Ctx

BINARY_SNIFF_BYTES = 8192
PROTECTED_DIRS = (".git", ".forge")


@dataclass
class TextFile:
    """Decoded file content plus what is needed to write it back the same way."""

    text: str  # decoded, BOM removed, original line endings kept
    newline: str  # "\n" or "\r\n", from the first line break
    bom: bool
    latin1: bool  # True when the bytes were not valid UTF-8


@dataclass
class FileChange:
    """One file to write (`content`) or delete (`content=None`)."""

    path: Path
    content: bytes | None


def resolve_path(cwd: Path, path: str) -> Path:
    """Resolve a tool's path argument against `cwd`; `~` and absolute paths are allowed."""
    candidate = Path(path).expanduser()
    if not candidate.is_absolute():
        candidate = cwd / candidate
    return candidate.resolve()


def display_path(root: Path, path: Path) -> str:
    """Root-relative path with forward slashes; paths outside the root stay absolute."""
    try:
        rel = path.relative_to(root)
    except ValueError:
        return path.as_posix()
    return rel.as_posix() or "."


def is_within(path: Path, folder: Path) -> bool:
    """True if `path` is `folder` or inside it."""
    return path == folder or folder in path.parents


def writable_roots(root: Path, cfg: ForgeConfig) -> list[Path]:
    """The project root plus `sandbox.writable_roots`, resolved."""
    extra = [Path(p).expanduser().resolve() for p in cfg.sandbox.writable_roots]
    return [root, *extra]


def check_writable(root: Path, roots: list[Path], path: Path) -> None:
    """Raise ToolError unless `path` may be written: inside a writable root and not protected."""
    display = display_path(root, path)
    owner = next((r for r in roots if is_within(path, r)), None)
    if owner is None:
        raise ToolError(
            "outside_root",
            f"{display} is outside the project",
            hint="only files inside the project or sandbox.writable_roots can be changed",
        )
    parts = path.relative_to(owner).parts
    if ".git" in parts or (owner == root and parts[:1] == (".forge",)):
        raise ToolError(
            "protected_path", f"{display} is protected", hint=".git/ and .forge/ are off limits"
        )


def format_size(size: int) -> str:
    """`412 B`, `4.1 KB`, `2.3 MB`."""
    if size < 1024:
        return f"{size} B"
    if size < 1024 * 1024:
        return f"{size / 1024:.1f} KB"
    return f"{size / (1024 * 1024):.1f} MB"


def is_binary(data: bytes) -> bool:
    """A file is binary if its first 8 KB contain a NUL byte."""
    return b"\0" in data[:BINARY_SNIFF_BYTES]


def decode_text(data: bytes) -> TextFile:
    """Decode UTF-8 (falling back to Latin-1) and detect the BOM and line ending style."""
    bom = data.startswith(b"\xef\xbb\xbf")
    body = data[3:] if bom else data
    try:
        text, latin1 = body.decode("utf-8"), False
    except UnicodeDecodeError:
        text, latin1 = body.decode("latin-1"), True
    first_break = text.find("\n")
    newline = "\r\n" if first_break > 0 and text[first_break - 1] == "\r" else "\n"
    return TextFile(text=text, newline=newline, bom=bom, latin1=latin1)


def encode_text(text: str, like: TextFile | None, keep_newlines: bool = False) -> bytes:
    """Encode `text` with the line endings, BOM and encoding of `like` (new files: LF UTF-8)."""
    if like is None:
        return text.encode("utf-8")
    if like.newline == "\r\n" and not keep_newlines:
        text = text.replace("\r\n", "\n").replace("\n", "\r\n")
    data = None
    if like.latin1:
        try:
            data = text.encode("latin-1")
        except UnicodeEncodeError:
            data = None  # new characters need UTF-8; better than refusing the write
    data = data if data is not None else text.encode("utf-8")
    return (b"\xef\xbb\xbf" if like.bom else b"") + data


def split_lines(text: str) -> list[str]:
    """Lines without their line endings; a final line break does not add an empty line."""
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    return [line.removesuffix("\r") for line in lines]


async def read_bytes(path: Path) -> bytes:
    """Read a file without blocking the event loop."""
    return await asyncio.to_thread(path.read_bytes)


def journal_dir(ctx: "Ctx") -> Path:
    """Folder of this session's undo journal."""
    return ctx.root / ".forge" / "undo" / ctx.session.id


async def apply_changes(ctx: "Ctx", changes: list[FileChange]) -> None:
    """Check, journal (for /undo), write atomically, and update the ledger — all or nothing."""
    roots = writable_roots(ctx.root, ctx.cfg)
    for change in changes:
        check_writable(ctx.root, roots, change.path)
    await asyncio.to_thread(_apply_sync, ctx.ledger, journal_dir(ctx), changes)


def _apply_sync(ledger: ReadLedger, journal: Path, changes: list[FileChange]) -> None:
    old = {c.path: (c.path.read_bytes() if c.path.is_file() else None) for c in changes}
    _journal(journal, old)
    done: list[Path] = []
    try:
        for change in changes:
            _write_one(change.path, change.content)
            done.append(change.path)
    except OSError:
        for path in done:  # put back what was already changed, then report the failure
            _write_one(path, old[path])
        raise
    for change in changes:
        if change.content is None:
            ledger.forget(change.path)
        else:
            ledger.record(change.path, change.content, change.path.stat().st_mtime_ns, full=True)


def _write_one(path: Path, content: bytes | None) -> None:
    if content is None:
        path.unlink(missing_ok=True)
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.parent / f".{path.name}.forge-{uuid.uuid4().hex[:8]}.tmp"
    temp.write_bytes(content)
    os.replace(temp, path)


def _journal(folder: Path, old: dict[Path, bytes | None]) -> None:
    """Append one undo record: the previous content of every file in this change."""
    folder.mkdir(parents=True, exist_ok=True)
    record = {
        str(path): None if data is None else base64.b64encode(data).decode("ascii")
        for path, data in old.items()
    }
    with (folder / "journal.jsonl").open("a", encoding="utf-8") as journal:
        journal.write(json.dumps(record) + "\n")


def undo_last_change(journal_dir: Path) -> list[Path]:
    """Restore the files of the most recent journal record; returns the paths restored."""
    journal = journal_dir / "journal.jsonl"
    if not journal.is_file():
        return []
    lines = journal.read_text(encoding="utf-8").splitlines()
    if not lines:
        return []
    record: dict[str, str | None] = json.loads(lines[-1])
    for name, data in record.items():
        _write_one(Path(name), None if data is None else base64.b64decode(data))
    journal.write_text("".join(line + "\n" for line in lines[:-1]), encoding="utf-8")
    return [Path(name) for name in record]
