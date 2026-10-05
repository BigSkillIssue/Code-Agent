"""The Forge/Codex patch format: strict parsing and in-memory hunk application (no file I/O).

*** Begin Patch
*** Add File: <path>         every following line starts with +
*** Delete File: <path>
*** Update File: <path>
*** Move to: <new path>      optional, directly after Update File
@@ <optional context header>
 <context>  -<removed>  +<added>
*** End of File              optional: the hunk must touch the end of the file
*** End Patch
"""

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Literal

from forge.runtime.errors import ToolError

BEGIN, END = "*** Begin Patch", "*** End Patch"
ADD, DELETE, UPDATE, MOVE = (
    "*** Add File: ",
    "*** Delete File: ",
    "*** Update File: ",
    "*** Move to: ",
)
END_OF_FILE = "*** End of File"

OpKind = Literal["add", "delete", "update"]
OPERATIONS: tuple[tuple[str, OpKind], ...] = ((ADD, "add"), (DELETE, "delete"), (UPDATE, "update"))


class PatchSyntaxError(Exception):
    """The patch text does not follow the format; `line` is 1-based."""

    def __init__(self, line: int, expected: str) -> None:
        super().__init__(f"line {line}: {expected}")
        self.line = line


@dataclass
class Hunk:
    """One change inside an Update: context, removed and added lines."""

    header: str = ""
    lines: list[tuple[str, str]] = field(default_factory=list)  # (" " | "-" | "+", text)
    end_of_file: bool = False

    @property
    def old(self) -> list[str]:
        """The lines the file must contain (context and removed lines)."""
        return [text for kind, text in self.lines if kind != "+"]

    @property
    def new(self) -> list[str]:
        """The lines that replace them (context and added lines)."""
        return [text for kind, text in self.lines if kind != "-"]


@dataclass
class FileOp:
    """One file operation of a patch."""

    kind: OpKind
    path: str
    move_to: str | None = None
    added: list[str] = field(default_factory=list)  # content of an Add
    hunks: list[Hunk] = field(default_factory=list)


def parse_patch(text: str) -> list[FileOp]:
    """Parse a patch strictly; raises PatchSyntaxError naming the first bad line."""
    lines = text.replace("\r\n", "\n").split("\n")
    first = next((i for i, line in enumerate(lines) if line.strip()), len(lines))
    last = max((i for i, line in enumerate(lines) if line.strip()), default=-1)
    if first > last or lines[first].strip() != BEGIN:
        raise PatchSyntaxError(first + 1, f"'{BEGIN}'")
    if lines[last].strip() != END:
        raise PatchSyntaxError(last + 1, f"'{END}' as the last line")
    ops: list[FileOp] = []
    for n in range(first + 1, last):
        _parse_line(ops, lines[n], n + 1)
    if not ops:
        raise PatchSyntaxError(last + 1, "at least one file operation before the end")
    return ops


def _parse_line(ops: list[FileOp], line: str, number: int) -> None:
    for prefix, kind in OPERATIONS:
        if line.startswith(prefix):
            path = line[len(prefix) :].strip()
            if not path:
                raise PatchSyntaxError(number, "a path after the colon")
            ops.append(FileOp(kind=kind, path=path))
            return
    op = ops[-1] if ops else None
    if op is None:
        raise PatchSyntaxError(number, "'*** Add File:', '*** Delete File:' or '*** Update File:'")
    if op.kind == "add":
        if not line.startswith("+"):
            raise PatchSyntaxError(number, "a line starting with '+' (content of the new file)")
        op.added.append(line[1:])
    elif op.kind == "delete":
        raise PatchSyntaxError(number, "the next file operation (Delete File takes no lines)")
    else:
        _parse_update_line(op, line, number)


def _parse_update_line(op: FileOp, line: str, number: int) -> None:
    if line.startswith(MOVE):
        if op.hunks or op.move_to is not None:
            raise PatchSyntaxError(number, "'*** Move to:' directly after '*** Update File:'")
        op.move_to = line[len(MOVE) :].strip()
    elif line.startswith("@@"):
        op.hunks.append(Hunk(header=line[2:].strip()))
    elif line.strip() == END_OF_FILE:
        if not op.hunks or not op.hunks[-1].lines:
            raise PatchSyntaxError(number, "'*** End of File' only after hunk lines")
        op.hunks[-1].end_of_file = True
    elif line == "" or line[0] in " -+":
        if not op.hunks or op.hunks[-1].end_of_file:
            op.hunks.append(Hunk())
        kind = line[0] if line else " "  # models often drop the space of empty context lines
        op.hunks[-1].lines.append((kind, line[1:]))
    else:
        raise PatchSyntaxError(number, "a hunk line starting with ' ', '-', '+' or '@@'")


# Matching levels, strictest first: exact, ignoring trailing whitespace, ignoring both ends.
LEVELS: tuple[Callable[[str], str], ...] = (lambda s: s, str.rstrip, str.strip)


def apply_hunks(lines: list[str], hunks: list[Hunk], display: str) -> list[str]:
    """Apply hunks in order; raises ToolError no_match / not_unique naming the hunk."""
    result = list(lines)
    start = 0
    for number, hunk in enumerate(hunks, start=1):
        at = find_hunk(result, hunk, start, f"{display} hunk {number}")
        replacement = replace_lines(result[at : at + len(hunk.old)], hunk)
        result[at : at + len(hunk.old)] = replacement
        start = at + len(replacement)
    return result


def replace_lines(matched: list[str], hunk: Hunk) -> list[str]:
    """The hunk's new lines, keeping context lines exactly as the file has them."""
    out: list[str] = []
    position = 0
    for kind, text in hunk.lines:
        if kind == "+":
            out.append(text)
            continue
        if kind == " ":
            out.append(matched[position])
        position += 1
    return out


def find_hunk(lines: list[str], hunk: Hunk, start: int, label: str) -> int:
    """Index where the hunk's old lines start."""
    if hunk.header:
        start = next(
            (i for i in range(start, len(lines)) if hunk.header.strip() in lines[i]), start
        )
    old = hunk.old
    if not old:
        return len(lines)  # pure insertion without context goes to the end
    for norm in LEVELS:
        wanted = [norm(line) for line in old]
        positions = [
            i
            for i in range(start, len(lines) - len(old) + 1)
            if [norm(line) for line in lines[i : i + len(old)]] == wanted
            and (not hunk.end_of_file or i + len(old) == len(lines))
        ]
        if len(positions) == 1:
            return positions[0]
        if len(positions) > 1:
            raise ToolError(
                "not_unique",
                f"{label}: the expected lines appear {len(positions)} times",
                hint="add more context lines or an @@ header",
                body=expected(old),
            )
    raise ToolError(
        "no_match",
        f"{label}: the expected lines were not found",
        hint="read the file again",
        body=expected(old),
    )


def expected(old: list[str]) -> str:
    """The hunk's expected lines, for error messages."""
    return "expected:\n" + "\n".join(f"  {line}" for line in old)
