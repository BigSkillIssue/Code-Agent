"""A new full-stack product (`forge app new`, S64): a FastAPI server on PostgreSQL with accounts,
moderation and migrations, its docs and CI, and the manifest hosting reads.

The files live as package data in `templates/<kind>/**.tmpl`, so Forge's own lint and type
checks never see the product's code. A path part `dot-x` becomes `.x`; `@@name@@` and
`@@title@@` in a file are the product's slug and its title.

Optional parts (S69): the files of a feature, such as `payments`, live in
`templates/<kind>/+<feature>/` and are added to the product, or replace the file of the same
path, only when it is asked for; lines between `@@if <feature>@@` and `@@end@@` (with an
optional `@@else@@`) are kept only with it, the lines after `@@else@@` only without it.
"""

import re
from collections.abc import Collection
from importlib import resources
from importlib.resources.abc import Traversable
from pathlib import Path

from forge.app_manifest import SLUG

KIND = "fullstack"
SUFFIX = ".tmpl"
NAME = re.compile(SLUG)
TITLE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 -]{0,59}$")  # safe in code, JSON, TOML and HTML
PLACEHOLDER = re.compile(r"@@([a-z_]+)@@")
FEATURES = ("payments",)  # optional parts: `forge app new --payments`
OPTIONAL = "+"  # the folder of a feature's own files: templates/<kind>/+<feature>/


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


def template_files(kind: str = KIND, features: Collection[str] = ()) -> dict[str, str]:
    """Every template file of a kind, with the files of the features asked for, as {path in
    the product: text with placeholders}."""
    unknown = sorted(set(features) - set(FEATURES))
    if unknown:
        raise ValueError(f"no such feature: {', '.join(unknown)}")
    root = resources.files("forge").joinpath("templates", kind)
    files: dict[str, str] = {}
    _collect(root, "", files)
    if not files:
        raise FileNotFoundError(f"no templates for {kind!r} in the forge package")
    for feature in sorted(features):
        _collect(root.joinpath(OPTIONAL + feature), "", files)
    return files


def _collect(folder: Traversable, prefix: str, files: dict[str, str]) -> None:
    """Add the template files below a folder to `files`."""
    for entry in sorted(folder.iterdir(), key=lambda item: item.name):
        if entry.name.startswith(OPTIONAL):
            continue  # a feature's files: added by template_files when asked for
        name = "." + entry.name[4:] if entry.name.startswith("dot-") else entry.name
        if entry.is_dir():
            _collect(entry, f"{prefix}{name}/", files)
        elif name.endswith(SUFFIX):
            files[prefix + name.removesuffix(SUFFIX)] = entry.read_text(encoding="utf-8")


def render(text: str, values: dict[str, str], features: Collection[str] = ()) -> str:
    """The text with its feature blocks resolved and its placeholders filled; an unknown
    placeholder or feature is a template bug."""
    return PLACEHOLDER.sub(lambda found: values[found.group(1)], blocks(text, features))


def blocks(text: str, features: Collection[str]) -> str:
    """The text with each `@@if feature@@ ... [@@else@@ ...] @@end@@` block resolved."""
    kept: list[str] = []
    block: list[bool] | None = None  # [the feature is asked for, in its @@else@@ part]
    for line in text.splitlines(keepends=True):
        marker = line.strip()
        if marker.startswith("@@if ") and marker.endswith("@@"):
            feature = marker[5:-2]
            if block is not None or feature not in FEATURES:
                raise ValueError(f"a nested block or an unknown feature: {marker}")
            block = [feature in features, False]
        elif marker == "@@else@@" and block is not None:
            block[1] = True
        elif marker == "@@end@@" and block is not None:
            block = None
        elif block is None or block[0] != block[1]:
            kept.append(line)
    if block is not None:
        raise ValueError("a block is not closed with @@end@@")
    return "".join(kept)


def app_files(
    name: str, title: str | None = None, kind: str = KIND, features: Collection[str] = ()
) -> dict[str, bytes]:
    """The product's files as {relative path: bytes}."""
    values = {"name": name, "title": title or title_of(name)}
    return {
        path: render(text, values, features).encode()
        for path, text in template_files(kind, features).items()
    }


def write_app(
    folder: Path, name: str, title: str | None = None, features: Collection[str] = ()
) -> list[Path]:
    """Write the product into `folder`, which must be missing or empty; return the files."""
    if folder.exists() and any(folder.iterdir()):
        raise FileExistsError(f"{folder} already exists and is not empty")
    written = []
    for relative, data in app_files(name, title, features=features).items():
        path = folder / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        written.append(path)
    return written
