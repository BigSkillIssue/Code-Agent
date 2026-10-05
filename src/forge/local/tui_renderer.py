"""Renderer for the Textual UI: stream pane, live plan, question picker, approval dialog.

The renderer runs in the app's event loop, so it updates widgets directly. Questions and
approvals open modal screens and wait for their result.
"""

import asyncio
import difflib
from typing import Any, TypeVar

from rich.syntax import Syntax
from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Label, OptionList, RichLog, SelectionList, Static

from forge.events import Event, ModelDelta, ModelDone, PlanUpdated, ToolStarted
from forge.local.rich_renderer import call_label, describe, plan_lines
from forge.plan import Question
from forge.ports import Answer, Approval
from forge.providers.base import ToolCall

OTHER = "Other…"
R = TypeVar("R")
DIFF_LINES = 60


class QuestionScreen(ModalScreen[list[str]]):
    """One question: options plus an "Other" row with free text, or a text field."""

    BINDINGS = [Binding("escape", "use_default", "Default")]

    def __init__(self, question: Question) -> None:
        super().__init__()
        self.question = question

    def compose(self) -> ComposeResult:
        """Question text, why it matters, then the picker."""
        q = self.question
        with Vertical(id="dialog"):
            yield Label(Text(q.text, style="bold"))
            if q.why:
                yield Label(Text(q.why, style="dim"))
            if q.kind == "multi":
                yield SelectionList[str](*[(o, o) for o in q.options], id="choices")
                yield Input(placeholder="Other (optional)", id="other")
                yield Button("OK", id="ok", variant="primary")
            elif q.kind in ("choice", "confirm"):
                options = q.options if q.kind == "choice" else ["yes", "no"]
                yield OptionList(*options, OTHER, id="choices")
                yield Input(placeholder="Type your answer", id="other", classes="hidden")
            else:
                yield Input(placeholder=q.default or "Type your answer", id="other")

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        """A picked option answers the question; "Other" opens the text field."""
        label = str(event.option.prompt)
        if label != OTHER:
            self.dismiss([label])
            return
        field = self.query_one("#other", Input)
        field.remove_class("hidden")
        field.focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        """Typed text answers the question (multi: together with the ticked options)."""
        if self.question.kind == "multi":
            self.finish_multi()
            return
        text = event.value.strip()
        self.dismiss([text] if text else self.default())

    def on_button_pressed(self, event: Button.Pressed) -> None:
        """OK on a multi-choice question."""
        self.finish_multi()

    def finish_multi(self) -> None:
        """The ticked options plus any typed "Other" text."""
        picked = list(self.query_one("#choices", SelectionList).selected)
        other = self.query_one("#other", Input).value.strip()
        self.dismiss([*picked, *([other] if other else [])] or self.default())

    def default(self) -> list[str]:
        """The question's default answer, if any."""
        return [self.question.default] if self.question.default else []

    def action_use_default(self) -> None:
        """Escape: take the default."""
        self.dismiss(self.default())


class ApprovalScreen(ModalScreen[Approval]):
    """Allow a tool call once, always, or refuse it with feedback."""

    BINDINGS = [
        Binding("y", "answer('yes')", "Yes"),
        Binding("a", "answer('always')", "Always"),
        Binding("n", "answer('no')", "No"),
    ]
    CHOICES = ("Yes", "Yes, and don't ask again this session", "No, tell Forge what to do instead")

    def __init__(self, call: ToolCall, reason: str) -> None:
        super().__init__()
        self.call = call
        self.reason = reason

    def compose(self) -> ComposeResult:
        """The call, the reason, a diff preview for edits, the choices."""
        with Vertical(id="dialog"):
            yield Label(Text(f"Allow {call_label(self.call)}?", style="bold yellow"))
            yield Label(Text(self.reason, style="dim"))
            preview = diff_for(self.call)
            if preview:
                yield Static(Syntax(preview, "diff", theme="ansi_dark"), id="preview")
            yield OptionList(*self.CHOICES, id="choices")
            yield Input(
                placeholder="What should Forge do instead? (Enter to send)",
                id="feedback",
                classes="hidden",
            )

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        """Map the picked row to an answer."""
        self.action_answer(("yes", "always", "no")[event.option_index])

    def action_answer(self, choice: str) -> None:
        """Yes / always end the dialog; no asks for optional feedback first."""
        if choice == "yes":
            self.dismiss(Approval(allow=True))
        elif choice == "always":
            self.dismiss(Approval(allow=True, remember=True))
        else:
            field = self.query_one("#feedback", Input)
            field.remove_class("hidden")
            field.focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        """Refuse, with the typed feedback for the model."""
        self.dismiss(Approval(allow=False, feedback=event.value.strip()))


class TuiRenderer:
    """The Renderer port for the Textual app (widgets #stream and #plan)."""

    def __init__(self, app: App[Any]) -> None:
        self.app = app
        self._pending = ""  # streamed text not yet ending in a line break

    @property
    def stream(self) -> RichLog:
        """The pane that shows the conversation."""
        return self.app.query_one("#stream", RichLog)

    async def show(self, event: Event) -> None:
        """Show one event."""
        if isinstance(event, ModelDelta):
            lines = (self._pending + event.text).split("\n")
            self._pending = lines.pop()
            for line in lines:
                self.stream.write(Text(line))
            return
        self.flush()
        if isinstance(event, ModelDone):
            return
        if isinstance(event, PlanUpdated):
            self.app.query_one("#plan", Static).update(Text(plan_lines(event.plan)))
            return
        self.stream.write(describe(event))
        if isinstance(event, ToolStarted) and (preview := diff_for(event.call)):
            self.stream.write(Syntax(preview, "diff", theme="ansi_dark"))

    def flush(self) -> None:
        """Write out the last partial line of streamed text."""
        if self._pending:
            self.stream.write(Text(self._pending))
            self._pending = ""

    def write(self, text: str, style: str = "") -> None:
        """Show a line that is not an event (command output, prompts)."""
        self.flush()
        self.stream.write(Text(text, style=style))

    async def ask(self, questions: list[Question]) -> list[Answer]:
        """Ask each question in a modal picker."""
        answers = []
        for index, question in enumerate(questions):
            values = await self.wait_for(QuestionScreen(question))
            answers.append(Answer(question_index=index, values=values))
        return answers

    async def approve(self, call: ToolCall, reason: str) -> Approval:
        """Ask in a modal dialog whether the call may run."""
        return await self.wait_for(ApprovalScreen(call, reason))

    async def wait_for(self, screen: ModalScreen[R]) -> R:
        """Push a modal screen and wait until it is dismissed."""
        future: asyncio.Future[R] = asyncio.get_running_loop().create_future()
        self.flush()
        self.app.push_screen(screen, callback=lambda result: future.set_result(result))
        return await future


def diff_for(call: ToolCall) -> str:
    """A diff preview of an edit (edit_file, write_file, apply_patch), or ''."""
    args = call.arguments
    if (
        call.name == "edit_file"
        and isinstance(args.get("old"), str)
        and isinstance(args.get("new"), str)
    ):
        path = str(args.get("path", ""))
        lines = difflib.unified_diff(
            args["old"].splitlines(), args["new"].splitlines(), path, path, lineterm=""
        )
        return cut("\n".join(lines))
    if call.name == "write_file" and isinstance(args.get("content"), str):
        body = "\n".join("+" + line for line in args["content"].splitlines())
        return cut(f"+++ {args.get('path', '')}\n{body}")
    if call.name == "apply_patch" and isinstance(args.get("patch"), str):
        return cut(args["patch"])
    return ""


def cut(text: str) -> str:
    """At most DIFF_LINES lines."""
    lines = text.splitlines()
    if len(lines) <= DIFF_LINES:
        return text
    return "\n".join([*lines[:DIFF_LINES], f"... {len(lines) - DIFF_LINES} more lines"])
