"""The agent's todo list (S54): items, the rules they follow and how they read as text."""

from typing import Literal

from pydantic import BaseModel

MAX_TODOS = 50
MARKS = {"completed": "[x]", "in_progress": "[>]", "pending": "[ ]"}


class Todo(BaseModel):
    """One item of an agent's todo list."""

    content: str  # imperative: "Run the tests"
    status: Literal["pending", "in_progress", "completed"]
    active_form: str = ""  # shown while in progress: "Running the tests"


def todo_problems(todos: list[Todo]) -> list[str]:
    """Why a list is not valid (empty list = valid)."""
    problems = []
    if len(todos) > MAX_TODOS:
        problems.append(f"at most {MAX_TODOS} todos")
    if sum(t.status == "in_progress" for t in todos) > 1:
        problems.append("at most one todo may be in_progress at a time")
    if any(not t.content.strip() or len(t.content) > 200 for t in todos):
        problems.append("every todo needs content of 1-200 characters")
    return problems


def todo_lines(todos: list[Todo]) -> list[str]:
    """A checklist; the item in progress shows its active form."""
    done = sum(t.status == "completed" for t in todos)
    lines = [f"todos: {done} of {len(todos)} done"]
    for todo in todos:
        text = (
            todo.active_form if todo.status == "in_progress" and todo.active_form else todo.content
        )
        lines.append(f"{MARKS[todo.status]} {text}")
    return lines
