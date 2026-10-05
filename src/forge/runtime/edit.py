"""Exact text replacement for edit_file, with helpful errors and context snippets."""

import difflib
from dataclasses import dataclass

from forge.runtime.errors import ToolError
from forge.runtime.files import split_lines
from forge.runtime.readers import numbered

CONTEXT_LINES = 3
MAX_SNIPPETS = 3


@dataclass
class Replacement:
    """The new text and the 1-based line where each replacement starts in it."""

    text: str
    start_lines: list[int]


def adapt_newlines(text: str, newline: str) -> str:
    """Write `text` with the file's line endings, whatever the model sent."""
    text = text.replace("\r\n", "\n")
    return text.replace("\n", "\r\n") if newline == "\r\n" else text


def line_of(text: str, index: int) -> int:
    """1-based line number of a character index."""
    return text.count("\n", 0, index) + 1


def find_all(text: str, needle: str) -> list[int]:
    """Start indices of every non-overlapping occurrence."""
    found, start = [], text.find(needle)
    while start != -1:
        found.append(start)
        start = text.find(needle, start + len(needle))
    return found


def replace_text(text: str, old: str, new: str, replace_all: bool, display: str) -> Replacement:
    """Replace one unique (or every) occurrence of `old`; ToolError when that is impossible."""
    positions = find_all(text, old)
    if not positions:
        raise ToolError(
            "no_match",
            f"the text to replace was not found in {display}",
            hint="these lines are the most similar; copy the exact text, including whitespace",
            body=similar_lines(text, old),
        )
    if len(positions) > 1 and not replace_all:
        where = ", ".join(str(line_of(text, p)) for p in positions)
        raise ToolError(
            "not_unique",
            f"the text to replace appears {len(positions)} times in {display}",
            hint=f"found at lines {where}; "
            "add surrounding lines to make it unique or set replace_all",
        )
    chosen = positions if replace_all else positions[:1]
    pieces, starts, cursor, shift = [], [], 0, 0
    for position in chosen:
        pieces += [text[cursor:position], new]
        starts.append(position + shift)
        shift += len(new) - len(old)
        cursor = position + len(old)
    new_text = "".join(pieces) + text[cursor:]
    return Replacement(new_text, [line_of(new_text, s) for s in starts])


def similar_lines(text: str, old: str, count: int = 3) -> str:
    """The lines most like the first non-blank line of `old`, numbered."""
    target = next((line.strip() for line in old.splitlines() if line.strip()), old.strip())
    lines = split_lines(text)
    scored = sorted(
        (
            (difflib.SequenceMatcher(None, target, line.strip()).ratio(), n)
            for n, line in enumerate(lines, 1)
        ),
        reverse=True,
    )[:count]
    return "\n".join(f"{n:>6}\t{lines[n - 1]}" for _, n in sorted(scored, key=lambda s: s[1]))


def edit_summary(display: str, replacement: Replacement, new: str) -> str:
    """`edited <path>: N replacement(s) at line(s) ...` plus context snippets."""
    starts = replacement.start_lines
    count = len(starts)
    noun = "replacement" if count == 1 else "replacements"
    where = ("line " if count == 1 else "lines ") + ", ".join(map(str, starts))
    out = [f"edited {display}: {count} {noun} at {where}"]
    lines = split_lines(replacement.text)
    height = new.replace("\r\n", "\n").count("\n")
    for start in starts[:MAX_SNIPPETS]:
        first = max(1, start - CONTEXT_LINES)
        last = min(len(lines), start + height + CONTEXT_LINES)
        if len(out) > 1:
            out.append("...")
        out.extend(numbered(lines[first - 1 : last], first))
    if count > MAX_SNIPPETS:
        out.append(f"[+ {count - MAX_SNIPPETS} more replacements]")
    return "\n".join(out)
