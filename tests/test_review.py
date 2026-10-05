"""Tests for the final review, run_task and the offline eval run."""

import json
import os
from pathlib import Path

import pytest

from forge.cli import main
from forge.config import ForgeConfig
from forge.pipeline import final_review, report_text, run_task
from forge.plan import Plan, Question, Step, TaskSpec
from forge.ports import Answer
from forge.providers.fake import FakeProvider, FakeToolCall, FakeTurn
from forge.providers.registry import register_provider
from support import ScriptedRenderer, git, make_ctx

REPO = Path(__file__).resolve().parents[1]
SPEC = TaskSpec(
    goal="Add a cache",
    context="",
    requirements=["r"],
    acceptance_criteria=["pytest passes"],
    assumptions=["Python 3.12"],
    size="medium",
)


def reviewer(ctx_root: Path, verdict: dict[str, object]) -> tuple[FakeProvider, ForgeConfig]:
    fake = FakeProvider(roles={"reviewer": [FakeTurn(text=json.dumps(verdict))]})
    cfg = ForgeConfig(roles={"reviewer": ["fake/reviewer"]})
    register_provider(cfg, fake)
    return fake, cfg


async def test_final_review_builds_the_report(tmp_project: Path) -> None:
    (tmp_project / "a.py").write_text("x = 1\n")
    git(tmp_project, "add", "-A")
    git(tmp_project, "commit", "-q", "-m", "base")
    (tmp_project / "a.py").write_text("x = 2\n")
    (tmp_project / "new.py").write_text("y = 1\n")
    fake, cfg = reviewer(
        tmp_project, {"ok": True, "summary": "Cache added.", "manual_checks": ["check the UI"]}
    )
    ctx = make_ctx(tmp_project, cfg=cfg)
    ctx.state.notes.append('Assumed for "TTL?": yes')
    plan = Plan(spec=SPEC, steps=[Step(id="s1", title="t", detail="", check="x", status="done")])
    report = await final_review(plan, ctx)
    assert report.ok and report.summary == "Cache added."
    assert report.files_changed == ["a.py", "new.py"]
    assert report.assumptions == ["Python 3.12", 'Assumed for "TTL?": yes']
    assert report.manual_checks == ["check the UI"]
    assert "-x = 1" in fake.requests[0].system and "pytest passes" in fake.requests[0].system
    assert "Check by hand: check the UI" in report_text(report)


async def test_unfinished_steps_make_the_report_fail(tmp_project: Path) -> None:
    _, cfg = reviewer(tmp_project, {"ok": True, "summary": "looks fine"})
    plan = Plan(spec=SPEC, steps=[Step(id="s1", title="t", detail="", check="x", status="failed")])
    assert not (await final_review(plan, make_ctx(tmp_project, cfg=cfg))).ok


async def test_report_lists_every_assumption_from_clarify(tmp_project: Path) -> None:
    question = Question(
        text="Which database?",
        kind="choice",
        options=["Redis", "SQLite"],
        default="Redis",
        why="dep",
    )
    spec = SPEC.model_copy(update={"open_questions": [question], "size": "medium"})
    steps = [{"title": "Add cache", "detail": "d", "check": "review: cache exists"}]
    fake = FakeProvider(
        roles={
            "refiner": [FakeTurn(text=spec.model_dump_json())],
            "planner": [
                FakeTurn(tool_calls=[FakeToolCall(name="submit_plan", arguments={"steps": steps})]),
                FakeTurn(text="planned"),
            ],
            "coder": [
                FakeTurn(
                    tool_calls=[
                        FakeToolCall(
                            name="write_file", arguments={"path": "cache.py", "content": "c = {}\n"}
                        )
                    ]
                ),
                FakeTurn(text="written"),
            ],
            "reviewer": [
                FakeTurn(text='{"pass": true, "reason": "ok"}'),
                FakeTurn(text='{"ok": true, "summary": "Added a cache."}'),
            ],
        }
    )
    cfg = ForgeConfig(roles={r: [f"fake/{r}"] for r in ("refiner", "planner", "coder", "reviewer")})
    register_provider(cfg, fake)
    ctx = make_ctx(
        tmp_project,
        cfg=cfg,
        headless=True,
        renderer=ScriptedRenderer(answers=[[Answer(question_index=0, values=["Redis"])]]),
    )
    report = await run_task("add a cache", ctx)
    assert report.ok, report
    assert 'Assumed for "Which database?": Redis' in report.assumptions
    assert "Python 3.12" in report.assumptions
    assert ctx.session.status == "done" and ctx.session.summary == "Added a cache."


def test_forge_eval_fake_runs_offline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    for name in list(os.environ):
        if name.startswith("FORGE_"):
            monkeypatch.delenv(name)
    monkeypatch.setenv("FORGE_HOME", str(tmp_path / "home"))
    monkeypatch.chdir(REPO)
    code = main(["--fake", "eval"])
    out = capsys.readouterr().out
    assert "passed 33/33" in out, out
    assert code == 0


def test_forge_eval_compare_modes_offline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    for name in list(os.environ):
        if name.startswith("FORGE_"):
            monkeypatch.delenv(name)
    monkeypatch.setenv("FORGE_HOME", str(tmp_path / "home"))
    monkeypatch.chdir(REPO)
    code = main(["--fake", "eval", "--suite", "large", "--compare", "solo,team"])
    out = capsys.readouterr().out
    assert "solo: passed 5/5" in out and "team: passed 5/5" in out, out
    assert "team does not pass more tasks than solo (5 vs 5)" in out
    assert code == 1  # the scripted model solves everything either way; the gate needs live runs
