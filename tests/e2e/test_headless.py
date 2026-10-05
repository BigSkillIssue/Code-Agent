"""`forge run --json`: one event per line, exit 0 done / 1 failed / 2 needs input."""

import json
import os
from pathlib import Path
from typing import Any

import pytest

from forge import events
from forge.cli import main
from support import init_repo

SPEC = {
    "goal": "Say hello",
    "context": "",
    "requirements": [],
    "acceptance_criteria": ["a greeting is printed"],
    "size": "trivial",
}


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    for name in list(os.environ):
        if name.startswith("FORGE_"):
            monkeypatch.delenv(name)
    monkeypatch.setenv("FORGE_HOME", str(tmp_path / "home"))
    root = init_repo(tmp_path / "project")
    monkeypatch.chdir(root)
    return root


def script(root: Path, data: dict[str, Any]) -> str:
    path = root.parent / "script.json"
    path.write_text(json.dumps(data))
    return str(path)


def run(
    root: Path, data: dict[str, Any], *flags: str, capsys: pytest.CaptureFixture[str]
) -> tuple[int, list[events.Event]]:
    code = main(["--fake", script(root, data), "run", "--json", *flags, "say hello"])
    lines = [line for line in capsys.readouterr().out.splitlines() if line.strip()]
    return code, [events.parse_event(line) for line in lines]


def test_done_prints_events_and_exits_0(project: Path, capsys: pytest.CaptureFixture[str]) -> None:
    data = {"roles": {"refiner": [{"text": json.dumps(SPEC)}]}, "turns": [{"text": "Hello there."}]}
    code, parsed = run(project, data, "--yes", capsys=capsys)
    assert code == 0
    kinds = [type(e).__name__ for e in parsed]
    assert "ModelDelta" in kinds and "ModelDone" in kinds
    done = parsed[-1]
    assert isinstance(done, events.SessionDone) and done.ok and "Hello there." in done.report
    assert all(e.kind in {  # type: ignore[attr-defined]
        "model_delta", "model_done", "tool_started", "tool_finished", "question",
        "plan_updated", "step_done", "compacted", "session_done", "error",
    } for e in parsed)  # fmt: skip


def test_failure_exits_1(project: Path, capsys: pytest.CaptureFixture[str]) -> None:
    data = {"roles": {"refiner": [{"text": json.dumps(SPEC)}]}, "turns": [{"error": "auth"}]}
    code, parsed = run(project, data, capsys=capsys)
    assert code == 1
    assert isinstance(parsed[-1], events.SessionDone) and not parsed[-1].ok


def test_question_without_defaults_exits_2(
    project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    question = {"text": "Which greeting?", "kind": "text", "why": "wording"}
    spec = {**SPEC, "size": "small", "open_questions": [question]}
    data = {"roles": {"refiner": [{"text": json.dumps(spec)}]}, "turns": []}
    code, parsed = run(project, data, "--no-defaults", capsys=capsys)
    assert code == 2
    asked = [e for e in parsed if isinstance(e, events.QuestionAsked)]
    assert asked and asked[0].questions[0].text == "Which greeting?"
    assert isinstance(parsed[-1], events.SessionDone) and parsed[-1].report == "needs input"


def test_questions_use_defaults_otherwise(
    project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    question = {"text": "Which greeting?", "kind": "text", "default": "hi", "why": "wording"}
    spec = {**SPEC, "open_questions": [question]}
    data = {"roles": {"refiner": [{"text": json.dumps(spec)}]}, "turns": [{"text": "hi"}]}
    code, parsed = run(project, data, capsys=capsys)
    assert code == 0 and isinstance(parsed[-1], events.SessionDone) and parsed[-1].ok
