"""Serialize plain nested dicts to TOML text (the standard library can only read TOML)."""

import json
import re
from collections.abc import Mapping, Sequence
from typing import Any

_BARE_KEY = re.compile(r"^[A-Za-z0-9_-]+$")


def dumps(data: Mapping[str, Any]) -> str:
    """Return `data` as a TOML document; keys whose value is None are left out."""
    lines: list[str] = []
    _emit_table(lines, [], data)
    return "\n".join(lines).strip() + "\n"


def _emit_table(lines: list[str], path: list[str], table: Mapping[str, Any]) -> None:
    scalars = [(k, v) for k, v in table.items() if v is not None and not _is_table(v)]
    tables = [(k, v) for k, v in table.items() if _is_table(v)]
    if path and (scalars or not tables):
        lines.append("")
        lines.append("[" + ".".join(_key(part) for part in path) + "]")
    for key, value in scalars:
        lines.append(f"{_key(key)} = {_value(value)}")
    for key, value in tables:
        _emit_table(lines, [*path, key], value)


def _is_table(value: Any) -> bool:
    # Empty mappings stay inline (`headers = {}`); TOML has no other way to show them.
    return isinstance(value, Mapping) and len(value) > 0


def _key(key: str) -> str:
    return key if _BARE_KEY.match(key) else json.dumps(key, ensure_ascii=False)


def _value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int | float):
        return repr(value)
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, Mapping):
        items = [f"{_key(k)} = {_value(v)}" for k, v in value.items() if v is not None]
        return "{ " + ", ".join(items) + " }" if items else "{}"
    if isinstance(value, Sequence):
        return "[" + ", ".join(_value(item) for item in value) + "]"
    raise TypeError(f"cannot write {type(value).__name__} as TOML")
