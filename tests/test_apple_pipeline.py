"""Apple checkpoints in run_task: the request, the plan and the built app are reviewed against
Apple's guidelines, and only an app the user approves is ready for Apple."""

import argparse
import json
from pathlib import Path
from typing import Any

from forge.apple_flow import APPROVE, GO_ON, SEND_BACK
from forge.cli import build_parser, load
from forge.config import AppleConfig, ForgeConfig
from forge.ctx import Ctx
from forge.events import GuidelineReview
from forge.pipeline import Report, report_text, run_task
from forge.plan import TaskSpec
from forge.ports import (
    Answer,
    AppleAction,
    AppleBuildResult,
    AppleIssue,
    ApplePlatform,
    AppleScreen,
)
from forge.providers.base import ImagePart
from forge.providers.fake import FakeProvider, FakeToolCall, FakeTurn
from forge.wiring import install_fake
from support import ScriptedRenderer, make_ctx

SHOT = ImagePart(media_type="image/png", data_b64="iVBORw0KGgo=")
SPEC = TaskSpec(goal="A tally counter app", context="", requirements=["counts taps"],
                acceptance_criteria=["the number goes up"], size="small")  # fmt: skip
STEPS = [{"title": "Counter screen", "detail": "a number and a button", "check": "review: done"}]
PROJECT = """\
name: Demo
targets:
  Demo_iOS:
    platform: iOS
    settings:
      base:
        TARGETED_DEVICE_FAMILY: "1,2"
  Demo_macOS:
    platform: macOS
  Demo_watchOS:
    platform: watchOS
"""
DEVICES = {"ios": "iPhone 16", "ipados": "iPad Pro 13-inch (M4)", "macos": "Mac",
           "watchos": "Apple Watch Series 10 (46mm)"}  # fmt: skip


class Builder:
    """A Mac that builds, tests and photographs; the first `failures` builds fail."""

    def __init__(self, failures: int = 0) -> None:
        self.calls: list[tuple[str, ...]] = []
        self.failures = failures

    async def build(
        self, platform: ApplePlatform, action: AppleAction, scheme: str | None = None
    ) -> AppleBuildResult:
        self.calls.append(("build", platform, action))
        name = f"Demo_{platform}"
        if self.failures:
            self.failures -= 1
            error = AppleIssue(severity="error", message="cannot find 'Counter' in scope")
            return AppleBuildResult(ok=False, platform=platform, action=action, scheme=name,
                                    issues=[error], log_tail="** BUILD FAILED **")  # fmt: skip
        if platform == "watchos" and action == "test":
            tail = "xcodebuild: error: Scheme Demo_watchOS is not currently configured for the "
            tail += "test action."
            return AppleBuildResult(ok=False, platform=platform, action=action, scheme=name,
                                    log_tail=tail)  # fmt: skip
        tests = 2 if action == "test" else 0
        return AppleBuildResult(ok=True, platform=platform, action=action, scheme=name,
                                tests_run=tests)  # fmt: skip

    async def screenshot(
        self, platform: ApplePlatform, device: str | None = None, dark: bool = False
    ) -> AppleScreen:
        self.calls.append(("screenshot", platform, "dark" if dark else "light"))
        return AppleScreen(platform=platform, device=DEVICES[platform],
                           dark=dark or platform == "watchos", image=SHOT)  # fmt: skip

    async def close(self) -> None:
        """Nothing to stop."""


def review(status: str = "ok", fix: str = "", summary: str = "") -> FakeTurn:
    """An Apple reviewer answer: one area with `status`, the rest ok."""
    findings = [{"area": a, "status": "ok", "reason": "fine"}
                for a in ("safety", "performance", "business", "design", "hig")]  # fmt: skip
    findings.append({"area": "legal", "status": status, "guideline": "5.1.1",
                     "reason": "data is sent without consent", "fix": fix})  # fmt: skip
    text = json.dumps({"summary": summary or f"Verdict {status}.", "findings": findings})
    return FakeTurn(text=text)


def submit_plan() -> list[FakeTurn]:
    call = FakeToolCall(name="submit_plan", arguments={"steps": STEPS})
    return [FakeTurn(tool_calls=[call]), FakeTurn(text="planned")]


def apple_run(
    root: Path,
    apple: list[FakeTurn],
    *,
    answers: list[list[str]] | None = None,
    planner: list[FakeTurn] | None = None,
    coder_extra: list[FakeTurn] | None = None,
    builder: Builder | None = None,
    review_on: bool = True,
    headless: bool = False,
) -> tuple[Ctx, FakeProvider]:
    (root / "project.yml").write_text(PROJECT)
    counter = {"path": "Shared/Counter.swift", "content": "struct C {}\n"}
    write = FakeToolCall(name="write_file", arguments=counter)
    roles: dict[str, list[Any]] = {
        "refiner": [FakeTurn(text=SPEC.model_dump_json())],
        "planner": planner or submit_plan(),
        "coder": [FakeTurn(tool_calls=[write]), FakeTurn(text="written"), *(coder_extra or [])],
        "reviewer": [
            FakeTurn(text='{"pass": true, "reason": "ok"}'),
            FakeTurn(text='{"ok": true, "summary": "Counter added."}'),
        ],
        "apple_reviewer": apple,
    }
    replies = [[Answer(question_index=0, values=[a]) for a in batch] for batch in answers or []]
    cfg = ForgeConfig(apple=AppleConfig(review=review_on))
    ctx = make_ctx(root, cfg=cfg, renderer=ScriptedRenderer(answers=replies), headless=headless)
    fake = install_fake(cfg, FakeProvider(roles=roles))
    ctx.state.apple = builder if builder is not None else Builder()
    return ctx, fake


def asked(ctx: Ctx) -> list[str]:
    renderer = ctx.renderer
    assert isinstance(renderer, ScriptedRenderer)
    return [q.text for batch in renderer.questions for q in batch]


def requests_of(fake: FakeProvider, role: str) -> list[str]:
    return ["\n".join(m.text() for m in r.messages) for r in fake.requests if r.model == role]


def reviews(ctx: Ctx) -> list[GuidelineReview]:
    return list(ctx.state.apple_reviews)


async def test_without_the_apple_option_nothing_is_reviewed(tmp_project: Path) -> None:
    ctx, fake = apple_run(tmp_project, [], review_on=False)
    report = await run_task("make a tally counter", ctx)
    assert report.ok and not report.ready_for_apple
    assert not requests_of(fake, "apple_reviewer") and not ctx.state.apple_screens


async def test_a_request_that_breaks_the_rules_stops_before_planning(tmp_project: Path) -> None:
    ctx, fake = apple_run(tmp_project, [review("violation", summary="Sells user data.")])
    report = await run_task("an app that sells its users' contacts", ctx)
    assert not report.ok and not report.ready_for_apple
    assert "Apple review of the prompt" in report.summary and "Sells user data." in report.summary
    assert not requests_of(fake, "refiner") and not requests_of(fake, "planner")
    assert any("Apple" in q for q in asked(ctx))  # the user was asked, and the default is stop


async def test_the_user_may_let_a_flagged_request_go_on(tmp_project: Path) -> None:
    ctx, _ = apple_run(tmp_project, [review("violation"), review(), review()],
                       answers=[[GO_ON], [APPROVE]])  # fmt: skip
    report = await run_task("a tally counter", ctx)
    assert report.ok and report.ready_for_apple
    assert any("despite the Apple review" in a for a in report.assumptions)


async def test_a_plan_that_breaks_the_rules_is_planned_again_with_the_fixes(
    tmp_project: Path,
) -> None:
    apple = [review(), review("violation", fix="ask for consent before sending data"), review(),
             review()]  # fmt: skip
    ctx, fake = apple_run(tmp_project, apple, planner=[*submit_plan(), *submit_plan()],
                          answers=[[APPROVE]])  # fmt: skip
    report = await run_task("a tally counter", ctx)
    assert report.ready_for_apple
    assert [r.stage for r in reviews(ctx)] == ["prompt", "plan", "plan", "product"]
    second_plan = [r for r in fake.requests if r.model == "planner"][2]
    assert "ask for consent before sending data" in second_plan.system


async def test_the_app_is_built_tested_photographed_reviewed_and_approved(
    tmp_project: Path,
) -> None:
    builder = Builder()
    ctx, fake = apple_run(tmp_project, [review(), review(), review(summary="Ready to ship.")],
                          builder=builder, answers=[[APPROVE]])  # fmt: skip
    report = await run_task("a tally counter", ctx)
    assert report.ok and report.ready_for_apple and "Ready for Apple: yes" in report_text(report)
    builds = [c for c in builder.calls if c[0] == "build"]
    assert builds == [("build", "ios", "test"), ("build", "macos", "test"),
                      ("build", "watchos", "test"), ("build", "watchos", "build")]  # fmt: skip
    shots = {c[1:] for c in builder.calls if c[0] == "screenshot"}
    assert shots == {("ios", "light"), ("ios", "dark"), ("ipados", "light"), ("ipados", "dark"),
                     ("macos", "light"), ("macos", "dark"), ("watchos", "dark")}  # fmt: skip
    saved = sorted(p.name for p in (tmp_project / ".forge" / "out" / "apple").glob("*.png"))
    assert saved == ["ios-dark.png", "ios-light.png", "ipados-dark.png", "ipados-light.png",
                     "macos-dark.png", "macos-light.png", "watchos-dark.png"]  # fmt: skip
    product = [r for r in fake.requests if r.model == "apple_reviewer"][2]
    assert sum(isinstance(p, ImagePart) for m in product.messages for p in m.parts) == 7
    assert "test for ios (scheme Demo_ios): succeeded" in requests_of(fake, "apple_reviewer")[2]
    assert any("Ready to ship." in q and ".forge/out/apple" in q for q in asked(ctx))


async def test_a_failing_build_goes_back_to_the_agent_before_any_review(
    tmp_project: Path,
) -> None:
    ctx, fake = apple_run(tmp_project, [review(), review(), review()], builder=Builder(failures=1),
                          coder_extra=[FakeTurn(text="fixed")], answers=[[APPROVE]])  # fmt: skip
    report = await run_task("a tally counter", ctx)
    assert report.ready_for_apple
    fix = requests_of(fake, "coder")[-1]
    assert "cannot find 'Counter' in scope" in fix
    assert [r.stage for r in reviews(ctx)] == ["prompt", "plan", "product"]  # once it was green


async def test_violations_in_the_app_go_back_to_the_agent_then_to_the_user(
    tmp_project: Path,
) -> None:
    bad = review("violation", fix="ask before sending data")
    fixed = [FakeTurn(text="fixed"), FakeTurn(text="fixed again")]
    ctx, fake = apple_run(tmp_project, [review(), review(), bad, bad, bad], coder_extra=fixed)
    report = await run_task("a tally counter", ctx)
    assert not report.ready_for_apple and "Apple review of the product" in report.apple_summary
    fixes = [t for t in requests_of(fake, "coder") if "ask before sending data" in t]
    assert len(fixes) == 2  # two rounds for the agent, then the user decided (default: stop)
    assert not any(APPROVE in q for q in asked(ctx))


async def test_the_user_can_send_the_app_back_with_feedback(tmp_project: Path) -> None:
    ctx, fake = apple_run(tmp_project, [review(), review(), review(), review()],
                          coder_extra=[FakeTurn(text="bigger now")],
                          answers=[[SEND_BACK], ["make the number bigger"], [APPROVE]])  # fmt: skip
    report = await run_task("a tally counter", ctx)
    assert report.ready_for_apple
    assert "make the number bigger" in requests_of(fake, "coder")[-1]
    assert [r.stage for r in reviews(ctx)] == ["prompt", "plan", "product", "product"]


async def test_nobody_approves_for_the_user(tmp_project: Path) -> None:
    ctx, _ = apple_run(tmp_project, [review(), review(), review()], headless=True)
    report = await run_task("a tally counter", ctx)
    assert report.ok and not report.ready_for_apple  # headless answers "not yet"
    assert "Ready for Apple: no" in report_text(report)


async def test_without_a_mac_the_app_is_reviewed_but_cannot_be_approved(
    tmp_project: Path,
) -> None:
    ctx, fake = apple_run(tmp_project, [review(), review(), review()])
    ctx.state.apple = None
    report = await run_task("a tally counter", ctx)
    assert not report.ready_for_apple and "Mac" in report.apple_summary
    assert "nothing was built" in requests_of(fake, "apple_reviewer")[2]
    assert not any(APPROVE in q for q in asked(ctx))


def test_the_apple_flag_turns_the_checks_on(tmp_project: Path) -> None:
    options: argparse.Namespace = build_parser().parse_args(["--apple", "-C", str(tmp_project)])
    _, cfg = load(options)
    assert cfg.apple.review
    plain = build_parser().parse_args(["-C", str(tmp_project)])
    assert not load(plain)[1].apple.review
    assert isinstance(Report.model_fields["ready_for_apple"].default, bool)
