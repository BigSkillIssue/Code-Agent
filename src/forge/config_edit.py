"""Edit TOML config files one table at a time; comments and other tables stay as they are.

Used by `forge mcp` and `forge ollama setup`, which change a few tables of a file the user
may also edit by hand.
"""

import re
import tomllib
from pathlib import Path
from typing import Any

from forge.toml_writer import dumps

HEADER = re.compile(r"^\s*\[\s*([^\[\]]+?)\s*\]\s*(#.*)?$")
KEY_PART = re.compile(r'"([^"]*)"|\'([^\']*)\'|([^.]+)')


def header_keys(header: str) -> list[str]:
    """The keys of a table header, quotes removed: `a."b.c"` -> ["a", "b.c"]."""
    keys = []
    for match in KEY_PART.finditer(header):
        quoted = match.group(1) if match.group(1) is not None else match.group(2)
        key = quoted if quoted is not None else match.group(3).strip()
        if key:
            keys.append(key)
    return keys


def remove_table(text: str, keys: list[str]) -> str:
    """The text without table `keys` and its sub-tables."""
    out: list[str] = []
    skipping = False
    for line in text.splitlines(keepends=True):
        header = HEADER.match(line)
        if header:
            skipping = header_keys(header.group(1))[: len(keys)] == keys
        if not skipping:
            out.append(line)
    return "".join(out)


def replace_table(text: str, keys: list[str], data: dict[str, Any]) -> str:
    """The text with table `keys` replaced by `data`, appended at the end."""
    kept = remove_table(text, keys).rstrip("\n")
    nested: dict[str, Any] = data
    for key in reversed(keys):
        nested = {key: nested}
    block = dumps(nested)
    return f"{kept}\n\n{block}" if kept else block


def parse_toml(text: str, path: Path) -> dict[str, Any]:
    """Parse TOML or explain which file is broken."""
    try:
        return tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise ValueError(f"{path} is not valid TOML: {exc}") from exc
