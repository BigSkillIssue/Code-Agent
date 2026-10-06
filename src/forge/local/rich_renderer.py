"""A plain terminal Renderer: streams text, shows tool calls, asks on stdin."""

import asyncio
import json
from typing import Any

from rich.console import Console
from rich.text import Text

from forge.events import (
    Compacted,
    ErrorEvent,
    Event,
    ModelDelta,
    ModelDone,
    PlanUpdated,
    SessionDone,
    StepDone,
    ToolFinished,
    ToolOutput,
    ToolStarted,
)
from forge.plan import Plan, Question
from forge.ports import Answer, Approval
from forge.providers.base import ToolCall

MARKS = {"todo": "[ ]", "doing": "[>]", "done": "[x]", "failed": "[!]", "skipped": "[-]"}
SETTLE_S = 0.02


class RichRenderer:
    """Shows events in the terminal and reads answers and approvals from stdin."""

    def __init__(self, console: Console | None = None, *, auto_approve: bool = False) -> None:
        self.console = console or Console(highlight=False)
        self.auto_approve = auto_approve
        self._mid_line = False

    async def show(self, event: Event) -> None:
        """Print one event."""
        if isinstance(event, ModelDelta):
            self.console.print(Text(event.text), end="")
            self._mid_line = not event.text.endswith("\n")
            return
        if self._mid_line:
            self.console.print()
            self._mid_line = False
        if isinstance(event, ModelDone):
            return
        self.console.print(describe(event))

    async def ask(self, questions: list[Question]) -> list[Answer]:
        """Ask each question; an empty answer takes the default."""
        await asyncio.sleep(SETTLE_S)  # let events published before this question print first
        answers = []
        for index, question in enumerate(questions):
            self.console.print(Text(f"? {question.text}", style="bold"))
            for number, option in enumerate(question.options, start=1):
                self.console.print(f"  {number}. {option}")
            if self.auto_approve:
                reply = ""
            else:
                reply = await self._read(
                    f"  answer{f' [{question.default}]' if question.default else ''}: "
                )
            answers.append(Answer(question_index=index, values=parse_answer(question, reply)))
        return answers

    async def approve(self, call: ToolCall, reason: str) -> Approval:
        """Ask whether a tool call may run: yes, no (with feedback) or always."""
        await asyncio.sleep(SETTLE_S)  # let events published before this question print first
        self.console.print(Text(f"? allow {call_label(call)}  ({reason})", style="yellow"))
        if self.auto_approve:
            return Approval(allow=True)
        reply = (await self._read("  [y]es / [n]o / [a]lways: ")).strip().lower()
        if reply in ("y", "yes"):
            return Approval(allow=True)
        if reply in ("a", "always"):
            return Approval(allow=True, remember=True)
        feedback = (await self._read("  what should it do instead? (optional): ")).strip()
        return Approval(allow=False, feedback=feedback)

    async def _read(self, prompt: str) -> str:
        try:
            return await asyncio.to_thread(self.console.input, prompt)
        except EOFError:
            return ""  # stdin is closed: no answer means the default, or "no" for approvals


def describe(event: Event) -> Text:
    """A one-line (or short) rendering of a non-text event."""
    if isinstance(event, ToolStarted):
        return Text(f"⏺ {call_label(event.call)}", style="cyan")
    if isinstance(event, ToolOutput):
        lines = event.text.rstrip("\n").split("\n")
        return Text("\n".join(f"  │ {line}" for line in lines), style="dim")
    if isinstance(event, ToolFinished):
        first = event.result.text.splitlines()[0] if event.result.text else ""
        style = "green" if event.result.ok else "red"
        return Text(f"  {'✓' if event.result.ok else '✗'} {first[:200]}", style=style)
    if isinstance(event, ErrorEvent):
        return Text(f"error: {event.message}", style="bold red")
    if isinstance(event, PlanUpdated):
        return Text(plan_lines(event.plan))
    if isinstance(event, StepDone):
        return Text(
            f"step {event.step_id} {'done' if event.ok else 'failed'}",
            style="green" if event.ok else "red",
        )
    if isinstance(event, Compacted):
        sizes = f"{event.tokens_before} -> {event.tokens_after} tokens"
        return Text(f"context compacted (level {event.level}): {sizes}", style="dim")
    if isinstance(event, SessionDone):
        return Text(f"{'done' if event.ok else 'stopped'}: {event.report}", style="bold")
    return Text(str(event.model_dump(exclude={"session_id", "ts"})), style="dim")


def plan_lines(plan: Plan) -> str:
    """The plan as a checklist."""
    lines = [f"plan (version {plan.version})"]
    lines += [f"{MARKS[step.status]} {step.id} {step.title}" for step in plan.steps]
    return "\n".join(lines)


def call_label(call: ToolCall) -> str:
    """`name(arg=value, ...)` with long values shortened."""
    return f"{call.name}({', '.join(f'{k}={short(v)}' for k, v in call.arguments.items())})"


def short(value: Any, limit: int = 60) -> str:
    """A value as JSON, cut to `limit` characters."""
    text = json.dumps(value, ensure_ascii=False)
    return text if len(text) <= limit else text[: limit - 3] + "..."


def parse_answer(question: Question, reply: str) -> list[str]:
    """Turn typed text into answer values: option numbers, option text, or free text."""
    reply = reply.strip()
    if not reply:
        return [question.default] if question.default else []
    if question.kind in ("choice", "multi"):
        picked = []
        for part in reply.split(",") if question.kind == "multi" else [reply]:
            part = part.strip()
            if part.isdigit() and 1 <= int(part) <= len(question.options):
                picked.append(question.options[int(part) - 1])
            else:
                picked.append(part)  # "Other": free text
        return picked
    if question.kind == "confirm":
        return ["yes" if reply.lower() in ("y", "yes") else "no"]
    return [reply]
