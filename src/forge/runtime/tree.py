"""Render a list of files as the folder tree that `list_dir` shows."""

from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

from forge.runtime.files import format_size

MAX_ENTRIES = 500


@dataclass
class Node:
    """A folder in the tree, with its subfolders and files (name -> size)."""

    folders: dict[str, "Node"] = field(default_factory=dict)
    files: dict[str, int] = field(default_factory=dict)


def build_tree(base: Path, files: list[Path], depth: int, folders: Sequence[Path] = ()) -> Node:
    """Nest `files` (and optional empty `folders`) under `base`, down to `depth` levels."""
    root = Node()
    for path in files:
        parts = path.relative_to(base).parts
        node = _descend(root, parts[:-1], depth)
        if node is not None and len(parts) <= depth:
            node.files[parts[-1]] = path.stat().st_size if path.exists() else 0
    for folder in folders:
        _descend(root, folder.relative_to(base).parts, depth)
    return root


def _descend(root: Node, parts: tuple[str, ...], depth: int) -> Node | None:
    node = root
    for level, part in enumerate(parts, start=1):
        if level > depth:
            return None
        node = node.folders.setdefault(part, Node())
    return node


def render_tree(title: str, root: Node, depth: int) -> str:
    """The tree text: folders first, then files, both case-insensitively sorted."""
    levels = "level" if depth == 1 else "levels"
    lines = [f"{title}/ ({depth} {levels})"]
    total = _count(root)
    _render(root, "", lines)
    shown = len(lines) - 1
    if total > shown:
        lines.append(f"[cut: {total - shown} more entries; use a deeper path or a lower depth]")
    return "\n".join(lines)


def _render(node: Node, prefix: str, lines: list[str]) -> None:
    entries: list[tuple[str, Node | int]] = [
        *sorted(node.folders.items(), key=lambda kv: kv[0].lower()),
        *sorted(node.files.items(), key=lambda kv: kv[0].lower()),
    ]
    for index, (name, value) in enumerate(entries):
        if len(lines) > MAX_ENTRIES:
            return
        last = index == len(entries) - 1
        branch = "└── " if last else "├── "
        if isinstance(value, Node):
            lines.append(f"{prefix}{branch}{name}/")
            _render(value, prefix + ("    " if last else "│   "), lines)
        else:
            lines.append(f"{prefix}{branch}{name}  {format_size(value)}")


def _count(node: Node) -> int:
    return len(node.files) + sum(1 + _count(child) for child in node.folders.values())
