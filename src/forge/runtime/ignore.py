"""The one list of project files that list_dir, glob, grep and repo_map all agree on.

In a git repo: tracked plus untracked-but-not-ignored files. Elsewhere: a walk that skips
a built-in list of tool folders. Hidden (dot) paths are left out unless asked for.
"""

import os
import re
from pathlib import Path

from forge.runtime.proc import run_argv, which

BUILTIN_IGNORES = frozenset(
    {
        ".git",
        "node_modules",
        ".venv",
        "venv",
        "__pycache__",
        "dist",
        "build",
        ".forge",
        ".mypy_cache",
        ".pytest_cache",
    }
)


async def project_files(
    base: Path, *, include_ignored: bool = False, hidden: bool = False
) -> list[Path]:
    """Absolute paths of the files under `base`, sorted by path."""
    if not base.is_dir():
        return [base] if base.is_file() else []
    files = None if include_ignored else await _git_files(base)
    if files is None:
        files = _walk(base, skip_ignored=not include_ignored)
    if not (hidden or include_ignored):
        files = [f for f in files if not is_hidden(f.relative_to(base))]
    return sorted(files)


def is_hidden(rel: Path) -> bool:
    """True if any part of a relative path starts with a dot."""
    return any(part.startswith(".") for part in rel.parts)


async def _git_files(base: Path) -> list[Path] | None:
    if which("git") is None:
        return None
    result = await run_argv(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"], base
    )
    if result.code != 0:
        return None  # not a git repository
    names = [n for n in result.stdout.split("\0") if n]
    return [p for p in (base / n for n in names) if p.is_file()]


def _walk(base: Path, *, skip_ignored: bool) -> list[Path]:
    found: list[Path] = []
    for folder, dirs, files in os.walk(base):
        dirs[:] = [d for d in dirs if d != ".git" and not (skip_ignored and d in BUILTIN_IGNORES)]
        found.extend(Path(folder) / name for name in files)
    return found


def expand_braces(pattern: str) -> list[str]:
    """`src/*.{ts,tsx}` -> [`src/*.ts`, `src/*.tsx`]; nested braces are expanded too."""
    start = pattern.find("{")
    if start == -1:
        return [pattern]
    depth = 0
    for end in range(start, len(pattern)):
        depth += {"{": 1, "}": -1}.get(pattern[end], 0)
        if depth == 0:
            break
    else:
        return [pattern]  # unbalanced: treat literally
    options = _split_top_level(pattern[start + 1 : end])
    head, tail = pattern[:start], pattern[end + 1 :]
    return [expanded for option in options for expanded in expand_braces(head + option + tail)]


def _split_top_level(body: str) -> list[str]:
    parts, depth, current = [], 0, ""
    for char in body:
        if char == "," and depth == 0:
            parts.append(current)
            current = ""
            continue
        depth += {"{": 1, "}": -1}.get(char, 0)
        current += char
    return [*parts, current]


def glob_regex(pattern: str) -> re.Pattern[str]:
    """Compile a glob (`*`, `?`, `[...]`, `**`) to a regex over forward-slash paths."""
    out, i = "", 0
    while i < len(pattern):
        if pattern.startswith("**/", i):
            out, i = out + "(?:.*/)?", i + 3
        elif pattern.startswith("**", i):
            out, i = out + ".*", i + 2
        elif pattern[i] == "*":
            out, i = out + "[^/]*", i + 1
        elif pattern[i] == "?":
            out, i = out + "[^/]", i + 1
        elif pattern[i] == "[" and "]" in pattern[i + 2 :]:
            end = pattern.index("]", i + 2)
            inner = pattern[i + 1 : end].replace("\\", "\\\\")
            out, i = (
                out + "[" + ("^" + inner[1:] if inner.startswith("!") else inner) + "]",
                end + 1,
            )
        else:
            out, i = out + re.escape(pattern[i]), i + 1
    return re.compile(out + r"\Z")


def matches_glob(rel: str, patterns: list[re.Pattern[str]]) -> bool:
    """True if a relative path matches any compiled glob."""
    return any(p.match(rel) for p in patterns)
