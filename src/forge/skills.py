"""Skills: Markdown instructions for specific jobs, in `<skills dir>/<name>/SKILL.md`.

Only each skill's name and description go into the system prompt; the agent reads the
whole file with `read_file` when a job needs it. Project skills (`.forge/skills/`) win over
user skills (`~/.forge/skills/`) with the same name.
"""

from dataclasses import dataclass
from pathlib import Path

from forge.config import forge_home


@dataclass
class Skill:
    """One skill: its name, what it is for, and where its instructions are."""

    name: str
    description: str
    path: Path


def skill_dirs(root: Path) -> list[Path]:
    """User folder first, project folder last (so project skills win)."""
    return [forge_home() / "skills", root / ".forge" / "skills"]


def discover(root: Path) -> list[Skill]:
    """Every readable skill, sorted by name."""
    found: dict[str, Skill] = {}
    for folder in skill_dirs(root):
        for path in sorted(folder.glob("*/SKILL.md")) if folder.is_dir() else []:
            try:
                skill = read_skill(path)
            except (OSError, UnicodeDecodeError):
                continue  # an unreadable skill is skipped; the others still work
            found[skill.name] = skill
    return sorted(found.values(), key=lambda s: s.name)


def read_skill(path: Path) -> Skill:
    """Name and description from the front matter (or the folder name and first line)."""
    text = path.read_text(encoding="utf-8").replace("\r\n", "\n")
    fields: dict[str, str] = {}
    body = text
    if text.startswith("---\n") and "\n---" in text[3:]:
        head, _, body = text[4:].partition("\n---")
        for line in head.splitlines():
            key, colon, value = line.partition(":")
            if colon:
                fields[key.strip().lower()] = value.strip().strip("'\"")
    first = next((line.strip("# ").strip() for line in body.splitlines() if line.strip(" -#")), "")
    return Skill(fields.get("name") or path.parent.name, fields.get("description") or first, path)


def skills_listing(root: Path) -> list[str]:
    """`- name: description (path)` lines; project paths relative, others absolute."""
    lines = []
    for skill in discover(root):
        shown = (
            skill.path.relative_to(root).as_posix()
            if skill.path.is_relative_to(root)
            else skill.path.as_posix()
        )
        lines.append(f"- {skill.name}: {skill.description[:200]} ({shown})")
    return lines
