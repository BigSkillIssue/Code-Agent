"""Decide whether a tool call runs, needs the user's approval, or is refused.

Stub until S28: `auto` tools run, `ask` tools ask, and approvals can be remembered.
"""

from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel

from forge.config import ForgeConfig

if TYPE_CHECKING:
    from forge.ctx import Ctx
    from forge.tools import ToolDef


class Decision(BaseModel):
    """The outcome of a permission check."""

    action: Literal["run", "ask", "deny"]
    reason: str


class Permissions:
    """Permission checks for one session."""

    def __init__(self, cfg: ForgeConfig) -> None:
        self.cfg = cfg
        self._remembered: set[tuple[str, str]] = set()

    def check(self, tool: "ToolDef", args: dict[str, Any], ctx: "Ctx") -> Decision:
        """Deny rules, ask rules, allow rules, read-only list, then sandbox mode x approval."""
        if (tool.name, specifier(tool, args)) in self._remembered:
            return Decision(action="run", reason="approved earlier in this session")
        if tool.permission == "ask":
            return Decision(action="ask", reason=f"{tool.name} needs approval")
        return Decision(action="run", reason="allowed by default")

    def remember(self, tool: "ToolDef", args: dict[str, Any]) -> None:
        """Allow this tool with this specifier for the rest of the session."""
        self._remembered.add((tool.name, specifier(tool, args)))


def specifier(tool: "ToolDef", args: dict[str, Any]) -> str:
    """The argument value that rules match against (command, path, url, role)."""
    if tool.specifier_arg is None:
        return ""
    return str(args.get(tool.specifier_arg, ""))
