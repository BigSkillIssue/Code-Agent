"""A Renderer for runs without a person: approves every call and answers with defaults."""

from forge.events import Event
from forge.plan import Question
from forge.ports import Answer, Approval
from forge.providers.base import ToolCall
from forge.questions import default_answer


class AutoRenderer:
    """Collects events, approves every tool call, and takes each question's default answer."""

    def __init__(self) -> None:
        self.events: list[Event] = []

    async def show(self, event: Event) -> None:
        """Keep the event (callers may inspect them afterwards)."""
        self.events.append(event)

    async def ask(self, questions: list[Question]) -> list[Answer]:
        """The default answer for every question."""
        return [
            Answer(question_index=i, values=[default_answer(q)]) for i, q in enumerate(questions)
        ]

    async def approve(self, call: ToolCall, reason: str) -> Approval:
        """Always allow."""
        return Approval(allow=True)
