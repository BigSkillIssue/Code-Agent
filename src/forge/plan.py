"""Task specs, questions, steps and plans: the data the pipeline works on."""

from typing import Literal

from pydantic import BaseModel


class Question(BaseModel):
    """A clarifying question for the user."""

    text: str
    kind: Literal["choice", "multi", "text", "confirm"]
    options: list[str] = []  # 2–6 for choice / multi
    default: str | None = None  # used in headless mode
    why: str  # one line: what changes with the answer


class TaskSpec(BaseModel):
    """The refined, testable description of what the user wants."""

    goal: str
    context: str
    requirements: list[str]
    constraints: list[str] = []
    acceptance_criteria: list[str]  # at least 1
    assumptions: list[str] = []
    open_questions: list[Question] = []
    size: Literal["trivial", "small", "medium", "large"]


StepStatus = Literal["todo", "doing", "done", "failed", "skipped"]


class Step(BaseModel):
    """One small, verifiable unit of work in a plan."""

    id: str  # "s1", "s2", ...
    title: str
    detail: str
    files: list[str] = []
    depends_on: list[str] = []
    check: str  # shell command, or "review: <criterion>"
    role: str = "coder"
    status: StepStatus = "todo"
    notes: str = ""
    attempts: int = 0


class Plan(BaseModel):
    """An ordered checklist of steps for a task spec."""

    spec: TaskSpec
    steps: list[Step]
    version: int = 1
