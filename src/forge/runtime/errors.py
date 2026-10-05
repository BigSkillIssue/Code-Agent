"""Shared tool error codes and the `error[<code>]: <message>` text format (docs/TOOLS.md)."""

from typing import Literal

from forge.providers.base import ToolResult

ErrorCode = Literal[
    "invalid_args",
    "not_found",
    "outside_root",
    "protected_path",
    "permission_denied",
    "sandbox_denied",
    "not_read",
    "stale",
    "no_match",
    "not_unique",
    "binary_file",
    "too_large",
    "timeout",
    "exit_nonzero",
    "check_failed",
    "http_status",
    "network",
    "unsupported",
    "busy",
    "limit_reached",
    "cancelled",
    "tool_error",
]


def error_text(code: ErrorCode, message: str, hint: str = "", body: str = "") -> str:
    """`error[<code>]: <message>`, then an optional `hint:` line and body."""
    lines = [f"error[{code}]: {message}"]
    if hint:
        lines.append(f"hint: {hint}")
    if body:
        lines.append(body)
    return "\n".join(lines)


def failure(
    code: ErrorCode, message: str, *, hint: str = "", body: str = "", call_id: str = ""
) -> ToolResult:
    """A failed ToolResult in the shared format."""
    return ToolResult(
        call_id=call_id, ok=False, text=error_text(code, message, hint, body), code=code
    )


class ToolError(Exception):
    """An expected tool failure raised inside tool helpers; call_tool turns it into a result.

    Raising keeps deep helper chains short; the model still only ever sees a ToolResult.
    """

    def __init__(self, code: ErrorCode, message: str, *, hint: str = "", body: str = ""):
        super().__init__(message)
        self.code: ErrorCode = code
        self.message = message
        self.hint = hint
        self.body = body

    def to_result(self, call_id: str = "") -> ToolResult:
        """This error as a failed ToolResult."""
        return failure(self.code, self.message, hint=self.hint, body=self.body, call_id=call_id)
