"""Decide whether a tool call runs, needs the user's approval, or is refused.

Order: deny rules -> ask rules -> allow rules (and approvals remembered this session) ->
built-in read-only commands -> sandbox mode x approval policy.
"""

import re
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel

from forge.config import ForgeConfig
from forge.ports import SandboxPolicy
from forge.runtime.rules import COMMAND_TOOLS, Rule, parse_rules, rule_matches
from forge.runtime.sandbox import launch_for

if TYPE_CHECKING:
    from forge.ctx import Ctx
    from forge.tools import ToolDef

READ_ONLY_COMMANDS = frozenset(
    {
        "ls", "cat", "head", "tail", "wc", "pwd", "echo", "which", "whoami", "date", "file",
        "stat", "du", "df", "tree", "rg", "grep", "egrep", "fgrep", "diff", "cmp", "sort",
        "uniq", "cut", "basename", "dirname", "realpath", "env", "printenv", "uname", "true",
        "get-childitem", "get-content", "get-location", "get-item", "select-string",
        "test-path", "resolve-path", "measure-object", "dir", "type",
    }
)  # fmt: skip
READ_ONLY_GIT = frozenset(
    {"status", "diff", "log", "show", "branch", "rev-parse", "ls-files", "blame"}
)
UNSAFE_SHELL = re.compile(r"[>`]|\$\(|&&|\|\||;|\n|\b(?:tee|xargs|sudo)\b")
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
        return self.decide(tool.name, specifier(tool, args), tool, ctx.root)

    def check_path(self, tool: "ToolDef", path: str, root: Path) -> Decision:
        """The same check for one path of a multi-file tool (apply_patch)."""
        return self.decide(tool.name, path, tool, root)

    def decide(self, name: str, spec: str, tool: "ToolDef", root: Path) -> Decision:
        """Evaluate the rules and defaults for one call of `name` with specifier `spec`."""
        if rule := first_match(self.deny, name, spec, root):
            return Decision(action="deny", reason=f"denied by rule {rule.text}")
        if rule := first_match(self.ask, name, spec, root):
            return Decision(action="ask", reason=f"rule {rule.text} asks first")
        if rule := first_match(self.allow, name, spec, root):
            return Decision(action="run", reason=f"allowed by rule {rule.text}")
        if (name, spec) in self._remembered:
            return Decision(action="run", reason="approved earlier in this session")
        if name in COMMAND_TOOLS and is_read_only_command(spec):
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


def is_read_only_command(command: str) -> bool:
    """True for simple commands (pipes allowed) whose every program only reads."""
    if not command.strip() or UNSAFE_SHELL.search(command):
        return False
    for segment in command.split("|"):
        words = segment.split()
        if not words:
            return False
        program = words[0].lower().rsplit("/", 1)[-1]
        if program == "git":
            if len(words) < 2 or words[1] not in READ_ONLY_GIT:
                return False
        elif program == "find":
            if any(w in ("-delete", "-exec", "-execdir", "-fprint", "-fls") for w in words):
                return False
        elif program not in READ_ONLY_COMMANDS:
            return False
    return True
