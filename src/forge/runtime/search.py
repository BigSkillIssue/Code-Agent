"""grep: ripgrep when it is installed, a Python fallback with the same output otherwise."""

import asyncio
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from forge.runtime.errors import ToolError
from forge.runtime.files import decode_text, display_path, is_binary
from forge.runtime.ignore import glob_regex, project_files
from forge.runtime.proc import run_argv, which

MAX_FILE_BYTES = 2 * 1024 * 1024
TYPE_GLOBS: dict[str, tuple[str, ...]] = {
    "py": ("*.py", "*.pyi"),
    "js": ("*.js", "*.mjs", "*.cjs", "*.jsx"),
    "ts": ("*.ts", "*.tsx", "*.mts", "*.cts"),
    "rust": ("*.rs",),
    "go": ("*.go",),
    "java": ("*.java",),
    "kotlin": ("*.kt", "*.kts"),
    "c": ("*.c", "*.h"),
    "cpp": ("*.cpp", "*.cc", "*.cxx", "*.hpp", "*.hh", "*.h"),
    "cs": ("*.cs",),
    "ruby": ("*.rb",),
    "php": ("*.php",),
    "swift": ("*.swift",),
    "sh": ("*.sh", "*.bash", "*.zsh"),
    "ps": ("*.ps1", "*.psm1", "*.psd1"),
    "md": ("*.md", "*.markdown"),
    "json": ("*.json",),
    "yaml": ("*.yaml", "*.yml"),
    "toml": ("*.toml",),
    "html": ("*.html", "*.htm"),
    "css": ("*.css", "*.scss", "*.sass", "*.less"),
    "sql": ("*.sql",),
}

Mode = Literal["files", "content", "count"]


@dataclass(frozen=True)
class GrepQuery:
    """What to search for and where."""

    pattern: str
    base: Path
    glob: str | None = None
    type: str | None = None
    mode: Mode = "files"
    context: int = 0
    case_insensitive: bool = False
    multiline: bool = False


@dataclass(frozen=True)
class Hit:
    """One matching line, or a context line next to one."""

    line: int
    text: str
    match: bool


Hits = dict[Path, list[Hit]]


async def search(query: GrepQuery, root: Path, use_rg: bool | None = None) -> Hits:
    """Hits per file; raises ToolError(invalid_args) for a bad regex or file type."""
    if use_rg is None:
        use_rg = which("rg") is not None
    if use_rg:
        return await _search_rg(query, root)
    return await _search_python(query)


async def _search_rg(query: GrepQuery, root: Path) -> Hits:
    argv = ["rg", "--json", "--max-filesize", "2M"]
    if query.case_insensitive:
        argv.append("-i")
    if query.multiline:
        argv += ["-U", "--multiline-dotall"]
    if query.glob:
        argv += ["-g", query.glob]
    if query.type:
        argv += ["-t", query.type]
    if query.mode == "content" and query.context:
        argv += ["-C", str(query.context)]
    argv += ["-e", query.pattern, "--", str(query.base)]
    result = await run_argv(argv, root, timeout_s=120)
    if result.code == 2 and ("regex" in result.stderr or "file type" in result.stderr):
        raise ToolError("invalid_args", result.stderr.strip().splitlines()[-1])
    hits: Hits = {}
    for line in result.stdout.splitlines():
        event = json.loads(line)
        if event.get("type") not in ("match", "context"):
            continue
        data = event["data"]
        text = data["lines"].get("text")
        if text is None or "text" not in data["path"]:
            continue  # not valid UTF-8
        path = Path(data["path"]["text"]).resolve()
        for offset, piece in enumerate(text.splitlines() or [""]):
            hits.setdefault(path, []).append(
                Hit(data["line_number"] + offset, piece, event["type"] == "match")
            )
    return hits


async def _search_python(query: GrepQuery) -> Hits:
    flags = re.IGNORECASE if query.case_insensitive else 0
    if query.multiline:
        flags |= re.MULTILINE | re.DOTALL
    try:
        regex = re.compile(query.pattern, flags)
    except re.error as exc:
        raise ToolError("invalid_args", f"invalid regex: {exc}") from exc
    if query.type and query.type not in TYPE_GLOBS:
        known = ", ".join(sorted(TYPE_GLOBS))
        raise ToolError(
            "invalid_args", f"unrecognized file type: {query.type}", hint=f"known types: {known}"
        )
    files = [f for f in await project_files(query.base) if _wanted(f, query)]
    return await asyncio.to_thread(_scan_files, files, regex, query)


def _wanted(path: Path, query: GrepQuery) -> bool:
    name = path.name
    rel = path.relative_to(query.base).as_posix() if query.base.is_dir() else name
    if query.glob and not glob_regex(query.glob).match(rel if "/" in query.glob else name):
        return False
    if query.type:
        return any(glob_regex(g).match(name) for g in TYPE_GLOBS[query.type])
    return True


def _scan_files(files: list[Path], regex: re.Pattern[str], query: GrepQuery) -> Hits:
    hits: Hits = {}
    for path in files:
        if path.stat().st_size > MAX_FILE_BYTES:
            continue
        data = path.read_bytes()
        if is_binary(data):
            continue
        lines = decode_text(data).text.replace("\r\n", "\n").split("\n")
        if lines and lines[-1] == "":
            lines.pop()  # a trailing newline does not make an extra line
        matched = _matching_lines(regex, lines, query.multiline)
        if matched:
            context = query.context if query.mode == "content" else 0
            hits[path] = _with_context(lines, matched, context)
    return hits


def _matching_lines(regex: re.Pattern[str], lines: list[str], multiline: bool) -> set[int]:
    if not multiline:
        return {i + 1 for i, line in enumerate(lines) if regex.search(line)}
    text = "\n".join(lines)
    found: set[int] = set()
    for match in regex.finditer(text):
        first = text.count("\n", 0, match.start()) + 1
        last = first + text.count("\n", match.start(), max(match.end() - 1, match.start()))
        found.update(range(first, last + 1))
    return found


def _with_context(lines: list[str], matched: set[int], context: int) -> list[Hit]:
    wanted: dict[int, bool] = {}
    for number in sorted(matched):
        for near in range(max(1, number - context), min(len(lines), number + context) + 1):
            wanted[near] = wanted.get(near, False) or near in matched
    return [Hit(n, lines[n - 1], is_match) for n, is_match in sorted(wanted.items())]


def format_hits(
    hits: Hits, query: GrepQuery, root: Path, *, offset: int = 0, head_limit: int = 200
) -> str:
    """The grep tool's output text, paged with offset and head_limit."""
    files = sorted(hits, key=lambda p: (-p.stat().st_mtime_ns, str(p)))
    if not files:
        return f"no matches for /{query.pattern}/ in {display_path(root, query.base)}"
    if query.mode == "files":
        lines = [display_path(root, f) for f in files]
    elif query.mode == "count":
        lines = [f"{display_path(root, f)}:{sum(h.match for h in hits[f])}" for f in files]
    else:
        lines = _content_lines(hits, files, root, separate=query.context > 0)
    window = lines[offset : offset + head_limit]
    out = list(window)
    if query.mode == "count":
        total = sum(h.match for f in files for h in hits[f])
        matches = _plural(total, "match", "matches")
        out.append(
            f"total: {total} {matches} in {len(files)} {_plural(len(files), 'file', 'files')}"
        )
    if offset + len(window) < len(lines):
        following = offset + len(window)
        out.append(
            f"[showing {len(window)} of {len(lines):,} results; use offset={following} for more]"
        )
    return "\n".join(out)


def _content_lines(hits: Hits, files: list[Path], root: Path, *, separate: bool) -> list[str]:
    lines: list[str] = []
    previous: tuple[Path, int] | None = None
    for path in files:
        name = display_path(root, path)
        for hit in sorted(hits[path], key=lambda h: h.line):
            if separate and previous is not None and previous != (path, hit.line - 1):
                lines.append("--")
            mark = ":" if hit.match else "-"
            lines.append(f"{name}{mark}{hit.line}{mark}{hit.text}")
            previous = (path, hit.line)
    return lines


def _plural(count: int, one: str, many: str) -> str:
    return one if count == 1 else many
