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

    def next_ready_step(self) -> Step | None:
        """First 'todo' step whose dependencies are all 'done' or 'skipped'."""
        ready = self.ready_steps()
        return ready[0] if ready else None

    def ready_steps(self) -> list[Step]:
        """All steps that could run now in parallel (used by teams)."""
        finished = {s.id for s in self.steps if s.status in ("done", "skipped")}
        return [s for s in self.steps if s.status == "todo" and set(s.depends_on) <= finished]

    def validate_graph(self) -> list[str]:
        """Errors: duplicate ids, unknown dependencies, cycles, missing checks. Empty = valid."""
        errors: list[str] = []
        ids = [s.id for s in self.steps]
        errors += [f"duplicate step id {i}" for i in sorted({i for i in ids if ids.count(i) > 1})]
        known = set(ids)
        for step in self.steps:
            if not step.check.strip():
                errors.append(f"{step.id} has no check")
            errors += [
                f"{step.id} depends on unknown step {d}" for d in step.depends_on if d not in known
            ]
            if step.id in step.depends_on:
                errors.append(f"{step.id} depends on itself")
        cycle = self._find_cycle()
        if cycle:
            errors.append("dependency cycle: " + " -> ".join(cycle))
        return errors

    def step(self, step_id: str) -> Step | None:
        """The step with this id, if any."""
        return next((s for s in self.steps if s.id == step_id), None)

    def _find_cycle(self) -> list[str]:
        edges = {s.id: [d for d in s.depends_on if d != s.id] for s in self.steps}
        state: dict[str, int] = {}  # 1 = on the current path, 2 = fully explored

        def visit(node: str, path: list[str]) -> list[str]:
            state[node] = 1
            for nxt in edges.get(node, []):
                if state.get(nxt) == 1:
                    return [*path[path.index(nxt) :], nxt] if nxt in path else [node, nxt]
                if nxt in edges and nxt not in state:
                    found = visit(nxt, [*path, nxt])
                    if found:
                        return found
            state[node] = 2
            return []

        for start in edges:
            if start not in state:
                found = visit(start, [start])
                if found:
                    return found
        return []


MARKS = {"todo": "[ ]", "doing": "[>]", "done": "[x]", "failed": "[!]", "skipped": "[-]"}


def checklist(plan: Plan) -> str:
    """The plan as a checklist: `[x] s1 Title (note)`."""
    lines = []
    for step in plan.steps:
        after = f" (after {', '.join(step.depends_on)})" if step.depends_on else ""
        note = (
            f" ({step.status}: {step.notes})"
            if step.status in ("skipped", "failed") and step.notes
            else ""
        )
        lines.append(f"{MARKS[step.status]} {step.id} {step.title}{after}{note}")
    return "\n".join(lines)
