"""Headless renderer for `forge run --json`: one contract event per line on stdout, no prompts.

Questions get their default answers (or, with --no-defaults, end the run as "needs input");
approvals are granted only with --yes, otherwise the model is told to find another way.
"""

import sys
from collections.abc import Callable
from typing import TextIO

from forge.events import Event
from forge.plan import Question
from forge.ports import Answer, Approval
from forge.providers.base import ToolCall
from forge.questions import default_answer

# The event kinds of docs/CONTRACTS.md; extra internal kinds are not part of the JSON stream.
CONTRACT_KINDS = frozenset(
    {
        "model_delta",
        "model_done",
        "tool_started",
        "tool_output",
        "tool_finished",
        "question",
        "plan_updated",
        "step_done",
        "compacted",
        "session_done",
        "error",
    }
)


class NeedsInput(Exception):
    """A question came up and --no-defaults forbids answering it automatically."""


class JsonRenderer:
    """Writes events as JSON lines; answers and approves without asking anyone."""

    def __init__(
        self,
        out: TextIO | None = None,
        *,
        auto_approve: bool = False,
        use_defaults: bool = True,
        on_needs_input: Callable[[], None] | None = None,
    ) -> None:
        self.out = out or sys.stdout
        self.auto_approve = auto_approve
        self.use_defaults = use_defaults
        self.on_needs_input = on_needs_input
        self.needed_input = False

    async def show(self, event: Event) -> None:
        """Print one contract event as a JSON line."""
        if getattr(event, "kind", "") in CONTRACT_KINDS:
            self.out.write(event.model_dump_json() + "\n")
            self.out.flush()

    async def ask(self, questions: list[Question]) -> list[Answer]:
        """Default answers, or stop the run when defaults are not allowed."""
        if not self.use_defaults:
            self.needed_input = True
            if self.on_needs_input is not None:
                self.on_needs_input()
            raise NeedsInput(questions[0].text if questions else "a question")
        return [
            Answer(question_index=i, values=[default_answer(q)]) for i, q in enumerate(questions)
        ]

    async def approve(self, call: ToolCall, reason: str) -> Approval:
        """Allow with --yes; otherwise refuse and say how to allow it."""
        if self.auto_approve:
            return Approval(allow=True)
        return Approval(
            allow=False,
            feedback=f"headless run: {reason}; no one can approve it. Use another approach, "
            "or the user can rerun with --yes or add an allow rule",
        )
