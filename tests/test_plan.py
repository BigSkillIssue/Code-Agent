"""Tests for the plan model's methods."""

from forge.plan import Plan, Step, TaskSpec, checklist

SPEC = TaskSpec(goal="g", context="c", requirements=[], acceptance_criteria=["a"], size="medium")


def plan(*steps: Step) -> Plan:
    return Plan(spec=SPEC, steps=list(steps))


def step(sid: str, *deps: str, status: str = "todo", check: str = "pytest") -> Step:
    return Step(
        id=sid, title=f"do {sid}", detail="", depends_on=list(deps), status=status, check=check
    )  # type: ignore[arg-type]


def test_next_ready_step_respects_dependencies_and_skips() -> None:
    p = plan(
        step("s1", status="done"),
        step("s2", "s3"),
        step("s3", "s1"),
        step("s4", "s5"),
        step("s5", status="skipped"),
    )
    assert p.next_ready_step() is not None and p.next_ready_step().id == "s3"  # type: ignore[union-attr]
    assert [s.id for s in p.ready_steps()] == ["s3", "s4"]


def test_no_ready_step_when_all_done_or_blocked() -> None:
    assert plan(step("s1", status="done")).next_ready_step() is None
    assert plan(step("s1", "s2"), step("s2", status="failed")).next_ready_step() is None


def test_validate_graph_finds_every_problem() -> None:
    p = plan(
        step("s1"),
        step("s1"),
        step("s2", "s9"),
        step("s3", check=" "),
        step("s4", "s5"),
        step("s5", "s4"),
    )
    errors = p.validate_graph()
    assert "duplicate step id s1" in errors
    assert "s2 depends on unknown step s9" in errors
    assert "s3 has no check" in errors
    assert any(e.startswith("dependency cycle:") and "s4" in e and "s5" in e for e in errors)


def test_valid_graph_has_no_errors() -> None:
    assert plan(step("s1"), step("s2", "s1"), step("s3", "s1", "s2")).validate_graph() == []


def test_self_dependency_is_an_error() -> None:
    assert "s1 depends on itself" in plan(step("s1", "s1")).validate_graph()


def test_ready_steps_lists_independent_steps() -> None:
    p = plan(step("s1"), step("s2"), step("s3", "s1"))
    assert [s.id for s in p.ready_steps()] == ["s1", "s2"]


def test_checklist() -> None:
    p = plan(step("s1", status="done"), step("s2", "s1", status="doing"))
    assert checklist(p) == "[x] s1 do s1\n[>] s2 do s2 (after s1)"
