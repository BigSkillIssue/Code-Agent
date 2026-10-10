"""A new full-stack product (`forge app new`, S64): a FastAPI server on PostgreSQL with accounts,
moderation and migrations, its docs and CI, and the manifest hosting reads.

The files live as package data in `templates/<kind>/**.tmpl`, so Forge's own lint and type
checks never see the product's code. A path part `dot-x` becomes `.x`; `@@name@@` and
`@@title@@` in a file are the product's slug and its title.
"""

import re
from importlib import resources
from importlib.resources.abc import Traversable
from pathlib import Path

from forge.app_manifest import SLUG

KIND = "fullstack"
SUFFIX = ".tmpl"
NAME = re.compile(SLUG)
TITLE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 -]{0,59}$")  # safe in code, JSON, TOML and HTML
PLACEHOLDER = re.compile(r"@@([a-z_]+)@@")


def name_problem(name: str) -> str | None:
    """Why a product name cannot be used, or None."""
    if NAME.match(name):
        return None
    return f"{name!r} is not a product name: lowercase letters, digits and dashes, 1-40 long"


def title_problem(title: str) -> str | None:
    """Why a product title cannot be used, or None."""
    if TITLE.match(title):
        return None
    return f"{title!r} is not a title: letters, digits, spaces and dashes, 1-60 long"


def title_of(name: str) -> str:
    """A human title from a slug: "tally-notes" → "Tally Notes"."""
    return " ".join(part.capitalize() for part in name.split("-") if part)


def template_files(kind: str = KIND) -> dict[str, str]:
    """Every template file of a kind as {path in the product: text with placeholders}."""
    root = resources.files("forge").joinpath("templates", kind)
    files: dict[str, str] = {}
    _collect(root, "", files)
    if not files:
        raise FileNotFoundError(f"no templates for {kind!r} in the forge package")
    return files


def _collect(folder: Traversable, prefix: str, files: dict[str, str]) -> None:
    """Add the template files below a folder to `files`."""
    for entry in sorted(folder.iterdir(), key=lambda item: item.name):
        name = "." + entry.name[4:] if entry.name.startswith("dot-") else entry.name
        if entry.is_dir():
            _collect(entry, f"{prefix}{name}/", files)
        elif name.endswith(SUFFIX):
            files[prefix + name.removesuffix(SUFFIX)] = entry.read_text(encoding="utf-8")


def render(text: str, values: dict[str, str]) -> str:
    """The text with its placeholders filled; an unknown placeholder is a template bug."""
    return PLACEHOLDER.sub(lambda found: values[found.group(1)], text)


def app_files(name: str, title: str | None = None, kind: str = KIND) -> dict[str, bytes]:
    """The product's files as {relative path: bytes}."""
    values = {"name": name, "title": title or title_of(name)}
    return {path: render(text, values).encode() for path, text in template_files(kind).items()}


def write_app(folder: Path, name: str, title: str | None = None) -> list[Path]:
    """Write the product into `folder`, which must be missing or empty; return the files."""
    if folder.exists() and any(folder.iterdir()):
        raise FileExistsError(f"{folder} already exists and is not empty")
    written = []
    for relative, data in app_files(name, title).items():
        path = folder / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        written.append(path)
    return written
