"""Project memory: FORGE.md, AGENTS.md and CLAUDE.md files that become part of the prompt.

Loaded most general first: ~/.forge/FORGE.md, then for each folder from the project root
down to the current folder its FORGE.md, AGENTS.md and CLAUDE.md. Later files are more
specific, so they win on conflict.
"""

from dataclasses import dataclass
from pathlib import Path

from forge.config import forge_home
from forge.runtime.files import display_path

MEMORY_NAMES = ("FORGE.md", "AGENTS.md", "CLAUDE.md")
MAX_BYTES = 32 * 1024


@dataclass(frozen=True)
class MemoryFile:
    """One loaded memory file and the folder tree its instructions apply to."""

    path: Path
    scope: str  # "user", or the root-relative folder ("." for the project root)
    text: str


def folders_down(root: Path, cwd: Path) -> list[Path]:
    """The root, then each folder on the way down to `cwd` (cwd outside root: root only)."""
    try:
        parts = cwd.relative_to(root).parts
    except ValueError:
        return [root]
    return [root.joinpath(*parts[:depth]) for depth in range(len(parts) + 1)]


def load_memory(root: Path, cwd: Path) -> list[MemoryFile]:
    """Every memory file that applies at `cwd`, most general first."""
    found: list[MemoryFile] = []
    user = forge_home() / "FORGE.md"
    if user.is_file():
        found.append(MemoryFile(user, "user", read_capped(user)))
    for folder in folders_down(root.resolve(), cwd.resolve()):
        for name in MEMORY_NAMES:
            path = folder / name
            if path.is_file():
                found.append(MemoryFile(path, display_path(root, folder), read_capped(path)))
    return found


def read_capped(path: Path) -> str:
    """The file's text, cut at 32 KB with a note saying so."""
    data = path.read_bytes()
    text = data[:MAX_BYTES].decode("utf-8", "replace")
    if len(data) > MAX_BYTES:
        text += f"\n[truncated: {path.name} is {len(data)} bytes; only the first 32 KB are shown]"
    return text


def render_memory(files: list[MemoryFile], root: Path) -> str:
    """The files as `<memory path=... scope=...>` blocks for the system prompt."""
    if not files:
        return "(none)"
    blocks = []
    for f in files:
        tag = f'<memory path="{display_path(root, f.path)}" scope="{f.scope}">'
        blocks.append(f"{tag}\n{f.text.strip()}\n</memory>")
    return "\n".join(blocks)
