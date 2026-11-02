"""Decide whether a tool call runs, needs the user's approval, or is refused.

Order: deny rules -> ask rules -> allow rules (and approvals remembered this session) ->
built-in read-only commands -> sandbox mode x approval policy.
"""

import re
import shlex
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel

from forge.config import ForgeConfig, forge_home
from forge.ports import SandboxPolicy
from forge.runtime.commandline import Found, commands_in, split_segments
from forge.runtime.rules import (
    COMMAND_TOOLS,
    PATH_TOOLS,
    Rule,
    absolute,
    parse_rules,
    rule_matches,
)
from forge.runtime.sandbox import launch_for

if TYPE_CHECKING:
    from forge.ctx import Ctx
    from forge.tools import ToolDef

# Programs that only read, called by their bare name (`./ls` could be anything). No wrapper
# that runs its arguments as a program (env, nice, timeout, xargs) belongs here.
READ_ONLY_COMMANDS = frozenset(
    {
        "ls", "cat", "head", "tail", "wc", "pwd", "echo", "which", "whoami", "date", "file",
        "stat", "du", "df", "tree", "rg", "grep", "egrep", "fgrep", "diff", "cmp", "sort",
        "uniq", "cut", "basename", "dirname", "realpath", "printenv", "uname", "true",
        "get-childitem", "get-content", "get-location", "get-item", "select-string",
        "test-path", "resolve-path", "measure-object", "dir", "type",
    }
)  # fmt: skip
READ_ONLY_GIT = frozenset({"status", "diff", "log", "show", "rev-parse", "ls-files", "blame"})
# `git branch` only lists with these options (and `-av` ...); the valued ones take the next word.
GIT_BRANCH_LISTING = frozenset(
    {"--all", "--remotes", "--list", "--verbose", "--ignore-case", "--show-current", "--color",
     "--no-color", "--column", "--no-column", "--abbrev", "--no-abbrev", "--omit-empty"}
)  # fmt: skip
GIT_BRANCH_FLAGS = re.compile(r"-[ailrv]+")
GIT_BRANCH_VALUED = frozenset(
    {"--contains", "--no-contains", "--merged", "--no-merged", "--points-at", "--sort", "--format"}
)
# Redirection, command and process substitution, background `&`, several commands in a line.
UNSAFE_SHELL = re.compile(r"[>`&;\n\r]|[$<]\(|\|\||\b(?:tee|xargs|sudo)\b")
# PowerShell also runs commands in `(...)`, `@(...)` and script blocks `{...}`.
UNSAFE_POWERSHELL = re.compile(r"[(){}]")
# find's actions that delete, write files or run programs.
FIND_ACTIONS = frozenset(
    {"-exec", "-execdir", "-ok", "-okdir", "-delete", "-fprint", "-fprint0", "-fprintf", "-fls"}
)
# Long options (also abbreviated, as getopt allows) that write files or run programs.
WRITING_LONG: dict[str, tuple[str, ...]] = {
    "sort": ("--output", "--compress-program"),
    "rg": ("--pre", "--hostname-bin"),
    "file": ("--compile",),
    "date": ("--set",),
}
# Short options that do the same (also inside `-no`), and the options whose value is the rest
# of the word, where looking stops (`date -Iseconds` sets nothing).
WRITING_SHORT: dict[str, tuple[str, str]] = {
    "sort": ("o", "kStT"),
    "tree": ("oR", ""),
    "file": ("C", "efFmP"),
    "date": ("s", "dfIr"),
}
WRITE_GROUPS = frozenset({"files", "memory"})


class Decision(BaseModel):
    """The outcome of a permission check."""

    action: Literal["run", "ask", "deny"]
    reason: str


class Permissions:
    """Permission checks for one session."""

    def __init__(self, cfg: ForgeConfig) -> None:
        self.cfg = cfg
        self.allow = parse_rules(cfg.permissions.allow)
        self.ask = parse_rules(cfg.permissions.ask)
        self.deny = parse_rules(cfg.permissions.deny)
        self._remembered: set[tuple[str, str]] = set()

    def check(self, tool: "ToolDef", args: dict[str, Any], ctx: "Ctx") -> Decision:
        """Deny rules, ask rules, allow rules, read-only list, then sandbox mode x approval."""
        return self.decide(tool.name, specifier(tool, args), tool, ctx.root, ctx.cwd)

    def check_path(self, tool: "ToolDef", path: str, root: Path) -> Decision:
        """The same check for one path of a multi-file tool (apply_patch)."""
        return self.decide(tool.name, path, tool, root)

    def decide(
        self, name: str, spec: str, tool: "ToolDef", root: Path, cwd: Path | None = None
    ) -> Decision:
        """Evaluate the rules and defaults for one call of `name` with specifier `spec`; a path
        is matched as written (from the agent's folder `cwd`) and with its links resolved:
        deny and ask rules catch either, an allow rule must cover both."""
        if name in COMMAND_TOOLS:
            return self.decide_command(name, spec, tool, root)
        forms = path_forms(spec, cwd or root) if name in PATH_TOOLS else [spec]
        if rule := first_match_any(self.deny, name, forms, root):
            return Decision(action="deny", reason=f"denied by rule {rule.text}")
        if rule := first_match_any(self.ask, name, forms, root):
            return Decision(action="ask", reason=f"rule {rule.text} asks first")
        allowing = [first_match(self.allow, name, form, root) for form in forms]
        if all(allowing):
            return Decision(action="run", reason=f"allowed by rule {allowing[0].text}")  # type: ignore[union-attr]
        if (name, spec) in self._remembered:
            return Decision(action="run", reason="approved earlier in this session")
        if tool.read_only and name in PATH_TOOLS and reads_outside(forms[-1], root, self.cfg):
            return Decision(action="ask", reason=f"{name} reads {spec}, outside the project")
        return self.default(tool)

    def decide_command(self, name: str, command: str, tool: "ToolDef", root: Path) -> Decision:
        """Deny/ask rules match if any command inside the line matches (`a; b`, `sh -c`,
        `$(...)`, `sudo` ...); allow rules only if every command inside is allowed."""
        if "\0" in command:
            # bash reads a command up to a NUL byte and runs what follows as a command of its own
            return Decision(action="deny", reason="the command contains a NUL byte")
        found = commands_in(command) or [Found(command, command)]
        candidates = [command, *(f.text for f in found), *(f.written for f in found)]
        if rule := first_match_any(self.deny, name, candidates, root):
            return Decision(action="deny", reason=f"denied by rule {rule.text}")
        if rule := first_match_any(self.ask, name, candidates, root):
            return Decision(action="ask", reason=f"rule {rule.text} asks first")
        allowing = [allowing_rule(self.allow, name, part, root) for part in found]
        if all(allowing):
            return Decision(action="run", reason=f"allowed by rule {allowing[0].text}")  # type: ignore[union-attr]
        if (name, command) in self._remembered:
            return Decision(action="run", reason="approved earlier in this session")
        if is_read_only_command(command, powershell=name == "powershell"):
            return Decision(action="run", reason="read-only command")
        return self.default(tool)

    def default(self, tool: "ToolDef") -> Decision:
        """Sandbox mode x approval policy for calls no rule covers."""
        mode, policy = self.cfg.sandbox.mode, self.cfg.approval.policy
        writes = not tool.read_only and tool.group in WRITE_GROUPS
        if mode == "read-only" and writes:
            return Decision(action="deny", reason="the sandbox is read-only")
        if tool.name in COMMAND_TOOLS:
            return self.shell_default()
        if policy == "always" and not tool.read_only:
            return Decision(action="ask", reason="approval policy 'always'")
        if tool.permission == "ask":
            if policy == "never":
                return Decision(
                    action="deny", reason=f"{tool.name} needs approval and the policy is 'never'"
                )
            return Decision(action="ask", reason=f"{tool.name} needs approval")
        return Decision(action="run", reason="allowed by default")

    def shell_default(self) -> Decision:
        """Commands run freely inside an OS sandbox; outside one they need approval."""
        policy = self.cfg.approval.policy
        if policy == "never":
            return Decision(action="run", reason="approval policy 'never'")
        if policy == "always":
            return Decision(action="ask", reason="approval policy 'always'")
        sandbox = SandboxPolicy(mode=self.cfg.sandbox.mode, network=self.cfg.sandbox.network)
        if launch_for(sandbox).mechanism != "none":
            return Decision(action="run", reason="runs inside the sandbox")
        return Decision(action="ask", reason="no OS sandbox: the command runs unrestricted")

    def remember(self, tool: "ToolDef", args: dict[str, Any]) -> None:
        """Allow this tool with this specifier for the rest of the session."""
        self._remembered.add((tool.name, specifier(tool, args)))


def first_match_any(rules: list[Rule], name: str, specs: list[str], root: Path) -> Rule | None:
    """The first rule matching any of the specifiers."""
    return next((r for r in rules for spec in specs if rule_matches(r, name, spec, root)), None)


def allowing_rule(rules: list[Rule], name: str, part: Found, root: Path) -> Rule | None:
    """The allow rule that covers one command; a command run with `sudo` or writing a file with
    `>` is only covered by a rule that names that as written (or by one allowing everything)."""
    if not part.marks:
        return first_match(rules, name, part.text, root)
    return next(
        (
            rule
            for rule in rules
            if rule_matches(rule, name, part.written, root)
            and (
                rule.pattern in (None, "", "*", "**") or all(m in rule.pattern for m in part.marks)
            )
        ),
        None,
    )


def path_forms(spec: str, cwd: Path) -> list[str]:
    """A path made absolute from the agent's folder, as written and with links resolved."""
    path = Path(spec or ".").expanduser()
    path = path if path.is_absolute() else cwd / path
    return list(dict.fromkeys([absolute(str(path), cwd), path.resolve().as_posix()]))


def reads_outside(spec: str, root: Path, cfg: ForgeConfig) -> bool:
    """True if a path (symlinks resolved) is outside the project, the writable roots and the
    user's Forge skills (which agents are told to read)."""
    target = Path(spec or ".").expanduser()
    target = (target if target.is_absolute() else root / target).resolve()
    allowed = [
        root.resolve(),
        *(Path(p).expanduser().resolve() for p in cfg.sandbox.writable_roots),
    ]
    allowed.append((forge_home() / "skills").resolve())
    return not any(target == base or target.is_relative_to(base) for base in allowed)


def first_match(rules: list[Rule], name: str, spec: str, root: Path) -> Rule | None:
    """The first rule that applies to the call."""
    return next((r for r in rules if rule_matches(r, name, spec, root)), None)


def specifier(tool: "ToolDef", args: dict[str, Any]) -> str:
    """The argument value that rules match against (command, path, `domain:<host>`, ...)."""
    if tool.specifier_arg is None:
        return ""
    value = str(args.get(tool.specifier_arg, ""))
    if tool.name == "web_fetch":
        return "domain:" + (urlsplit(value).hostname or "")
    return value


def is_read_only_command(command: str, powershell: bool = False) -> bool:
    """True for simple commands (pipes allowed) whose every program, with these options, only
    reads."""
    unsafe = UNSAFE_SHELL.search(command) or (powershell and UNSAFE_POWERSHELL.search(command))
    if not command.strip() or unsafe or "\0" in command:
        return False
    for segment in split_segments(command):
        try:
            words = shlex.split(segment)
        except ValueError:  # unbalanced quotes
            return False
        if not words or not program_only_reads(words[0].lower(), words[1:]):
            return False
    return True


def program_only_reads(program: str, args: list[str]) -> bool:
    """True if this program, called with these arguments, neither writes nor runs anything."""
    if program == "git":
        return bool(args) and git_only_reads(args[0], args[1:])
    if program == "find":
        return not any(word in FIND_ACTIONS for word in args)
    if program not in READ_ONLY_COMMANDS:
        return False
    if program == "uniq":  # `uniq IN OUT` writes OUT
        return len([word for word in args if not word.startswith("-")]) < 2
    letters, valued = WRITING_SHORT.get(program, ("", ""))
    return not any(
        names_option(word, WRITING_LONG.get(program, ())) or short_option(word, letters, valued)
        for word in args
    )


def git_only_reads(subcommand: str, args: list[str]) -> bool:
    """`git status`, `log`, ... and `git branch` when it only lists; diff, log and show must not
    write their output to a file (`--output`)."""
    if subcommand == "branch":
        return branch_only_lists(args)
    if subcommand not in READ_ONLY_GIT:
        return False
    return not any(names_option(word, ("--output",)) for word in args)


def branch_only_lists(args: list[str]) -> bool:
    """`git branch` with listing options only; patterns only after `--list` (a bare name
    makes a branch, and -d, -D, -m, -f ... delete, move or overwrite one)."""
    rest = list(args)
    while rest:
        word = rest.pop(0)
        name = word.split("=", 1)[0]
        if name in GIT_BRANCH_VALUED:
            if "=" not in word and rest and not rest[0].startswith("-"):
                rest.pop(0)  # the option's value
        elif name in GIT_BRANCH_LISTING or GIT_BRANCH_FLAGS.fullmatch(word):
            continue
        elif word.startswith("-") or "--list" not in args:
            return False
    return True


def names_option(word: str, options: tuple[str, ...]) -> bool:
    """True for `--name`, `--name=value` or an abbreviation (`--out=x`) of one of the options."""
    name = word.split("=", 1)[0]
    return name.startswith("--") and len(name) > 2 and any(o.startswith(name) for o in options)


def short_option(word: str, letters: str, valued: str) -> bool:
    """True if a short-option word (`-o`, `-no`) holds one of the letters before an option that
    takes the rest of the word as its value."""
    if not word.startswith("-") or word.startswith("--"):
        return False
    for char in word[1:]:
        if char in letters:
            return True
        if char in valued:
            return False
    return False
