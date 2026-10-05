"""Permission rules in Claude Code's `tool(specifier)` syntax, e.g. `bash(git status*)`.

- `bash(...)` / `powershell(...)`: a glob over the command line (`*` matches anything,
  a trailing `:*` is a prefix match as in Claude Code).
- File tools: a path glob resolved against the project root (`./.env*`, `src/**`, `~/.ssh/**`).
  A rule on `read_file` also covers the tools that write files.
- `web_fetch(domain:<host>)`: the URL's host; `*.example.com` covers subdomains.
- A bare tool name (`web_search`) matches every call of that tool.
"""

import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

COMMAND_TOOLS = frozenset({"bash", "powershell"})
PATH_TOOLS = frozenset(
    {"read_file", "write_file", "edit_file", "apply_patch", "list_dir", "glob", "grep", "repo_map"}
)
WRITE_TOOLS = frozenset({"write_file", "edit_file", "apply_patch"})
RULE = re.compile(r"^\s*([A-Za-z0-9_.-]+)\s*(?:\((.*)\))?\s*$", re.DOTALL)


class RuleError(ValueError):
    """A rule that cannot be parsed."""


@dataclass(frozen=True)
class Rule:
    """One parsed rule; `pattern` None matches every call of the tool."""

    text: str
    tool: str
    pattern: str | None


def parse_rule(text: str) -> Rule:
    """Parse `tool(specifier)` or a bare `tool`."""
    found = RULE.match(text)
    if found is None:
        raise RuleError(f"not a permission rule: {text!r} (expected tool(specifier))")
    pattern = found.group(2)
    return Rule(
        text=text,
        tool=found.group(1).lower(),
        pattern=pattern.strip() if pattern is not None else None,
    )


def parse_rules(texts: list[str]) -> list[Rule]:
    """Parse a list of rules."""
    return [parse_rule(t) for t in texts]


def rule_matches(rule: Rule, tool: str, specifier: str, root: Path) -> bool:
    """True if the rule applies to a call of `tool` with this specifier."""
    if not applies_to(rule.tool, tool):
        return False
    if rule.pattern is None or rule.pattern in ("", "*", "**"):
        return True
    if tool in COMMAND_TOOLS:
        return command_matches(rule.pattern, specifier)
    if tool in PATH_TOOLS:
        return path_matches(rule.pattern, specifier, root)
    if tool == "web_fetch":
        return domain_matches(rule.pattern, specifier)
    return glob_to_regex(rule.pattern, any_char=True).fullmatch(specifier) is not None


def applies_to(rule_tool: str, tool: str) -> bool:
    """A read_file rule also covers the tools that write files."""
    return rule_tool == tool or (rule_tool == "read_file" and tool in WRITE_TOOLS)


def command_matches(pattern: str, command: str) -> bool:
    """Glob over the whole command line; `prefix:*` means the command starts with prefix."""
    command = " ".join(command.split())
    if pattern.endswith(":*"):
        prefix = " ".join(pattern[:-2].split())
        return command == prefix or command.startswith(prefix + " ")
    return glob_to_regex(" ".join(pattern.split()), any_char=True).fullmatch(command) is not None


def path_matches(pattern: str, path: str, root: Path) -> bool:
    """Path glob (`*` within a folder, `**` across folders), both sides made absolute."""
    target = absolute(path, root)
    # a pattern starting with ** matches at any depth, so it is not anchored at the root
    expanded = pattern if pattern.startswith("**") else absolute(pattern, root)
    regex = glob_to_regex(expanded, any_char=False)
    return regex.fullmatch(target) is not None or regex.fullmatch(target + "/") is not None


def absolute(path: str, root: Path) -> str:
    """Forward-slash absolute form of a path or pattern (relative ones start at the root)."""
    candidate = Path(path).expanduser()
    if not candidate.is_absolute():
        candidate = root / candidate
    text = candidate.as_posix()
    # resolve '..' and './' without touching the disk (patterns contain '*')
    parts: list[str] = []
    for part in text.split("/"):
        if part == "..":
            if parts:
                parts.pop()
        elif part != ".":
            parts.append(part)
    return "/".join(parts) or "/"


def domain_matches(pattern: str, specifier: str) -> bool:
    """`domain:docs.python.org` or `domain:*.python.org` against a URL or `domain:<host>`."""
    host = specifier.removeprefix("domain:")
    if "://" in host:
        host = urlsplit(host).hostname or ""
    wanted = pattern.removeprefix("domain:").lower()
    host = host.lower()
    if wanted.startswith("*."):
        return host == wanted[2:] or host.endswith(wanted[1:])
    return host == wanted


def glob_to_regex(pattern: str, *, any_char: bool) -> re.Pattern[str]:
    """Compile a glob; with any_char `*` also crosses `/` (commands, plain strings)."""
    out = ""
    i = 0
    while i < len(pattern):
        char = pattern[i]
        if pattern.startswith("**/", i) and not any_char:
            out += "(?:.*/)?"
            i += 3
            continue
        if pattern.startswith("**", i):
            out += ".*"
            i += 2
            continue
        out += (
            (".*" if any_char else "[^/]*")
            if char == "*"
            else "."
            if char == "?"
            else re.escape(char)
        )
        i += 1
    return re.compile(out, re.DOTALL)
