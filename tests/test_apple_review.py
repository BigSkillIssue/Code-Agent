"""The independent Apple reviewer: a fresh context, read-only tools, a verdict per area."""

import json
from pathlib import Path
from typing import Any

from forge.apple_review import AREAS, review_plan, review_product, review_prompt
from forge.ctx import Ctx
from forge.events import GuidelineFinding, GuidelineReview, parse_event
from forge.local.rich_renderer import describe
from forge.plan import Plan, Step, TaskSpec
from forge.ports import AppleScreen
from forge.providers.base import (
    Capabilities,
    ImagePart,
    Message,
    text_message,
)
from forge.providers.fake import FakeProvider, FakeTurn
from forge.wiring import install_fake
from support import drain, make_ctx

SHOT = ImagePart(media_type="image/png", data_b64="iVBORw0KGgo=")
APPLE = "https://developer.apple.com/app-store/review/guidelines/"


def verdict(*findings: dict[str, Any], summary: str = "Fine for the App Store.") -> FakeTurn:
    """A reviewer answer: the given findings, every other area ok."""
    named = {f["area"] for f in findings}
    ok = [{"area": a, "status": "ok", "reason": "nothing to object to"} for a in AREAS
          if a not in named]  # fmt: skip
    body = {"summary": summary, "findings": [*findings, *ok], "sources": [APPLE]}
    return FakeTurn(text=f"```json\n{json.dumps(body)}\n```")


def reviewer_ctx(root: Path, *turns: FakeTurn, vision: bool = True) -> tuple[Ctx, FakeProvider]:
    ctx = make_ctx(root)
    caps = Capabilities(context_window=128_000, max_output=8_192, vision=vision)
    fake = install_fake(ctx.cfg, FakeProvider(roles={"apple_reviewer": list(turns)}, caps=caps))
    return ctx, fake


def request_text(fake: FakeProvider, index: int = 0) -> str:
    return "\n".join(m.text() for m in fake.requests[index].messages)


async def test_a_prompt_review_judges_every_guideline_area(tmp_project: Path) -> None:
    ctx, fake = reviewer_ctx(tmp_project, verdict())
    events = ctx.bus.subscribe("*")
    review = await review_prompt(ctx, "A tally counter for iPhone, iPad, Mac and Apple Watch")
    assert review.stage == "prompt" and review.verdict == "ok" and not review.error
    assert {f.area for f in review.findings} == set(AREAS) and review.sources == [APPLE]
    request = fake.requests[0]
    assert request.model == "apple_reviewer"
    assert "A tally counter for iPhone" in request_text(fake)
    assert "App Store Review Guidelines" in request.system and APPLE in request.system
    published = [e for e in await drain(events) if isinstance(e, GuidelineReview)]
    assert published == [review]


async def test_the_reviewer_sees_nothing_of_the_builders_work(tmp_project: Path) -> None:
    ctx, fake = reviewer_ctx(tmp_project, verdict())
    ctx.session.messages += [text_message("user", "build it"),
                             text_message("assistant", "I will hide the tracking SDK")]  # fmt: skip
    await review_prompt(ctx, "A tally counter")
    assert "tracking SDK" not in request_text(fake) and "build it" not in request_text(fake)
    assert "XcodeGen" not in fake.requests[0].system  # the builder's guidance is not its own


async def test_the_reviewer_may_read_and_fetch_but_never_change(tmp_project: Path) -> None:
    ctx, fake = reviewer_ctx(tmp_project, verdict())
    await review_prompt(ctx, "A tally counter")
    tools = {t.name for t in fake.requests[0].tools}
    assert {"read_file", "grep", "list_dir", "web_fetch"} <= tools
    changing = {"write_file", "edit_file", "bash", "apple_build", "apple_screenshot", "ask_user",
                "spawn_agent", "research"}  # fmt: skip
    assert not tools & changing


async def test_the_worst_finding_is_the_verdict(tmp_project: Path) -> None:
    payments = {"area": "business", "status": "violation", "guideline": "3.1.1",
                "reason": "premium themes are sold through a web checkout",
                "fix": "sell them with in-app purchase"}  # fmt: skip
    login = {"area": "design", "status": "concern", "guideline": "4.8",
             "reason": "Google login is planned",
             "fix": "offer Sign in with Apple too"}  # fmt: skip
    ctx, _ = reviewer_ctx(tmp_project, verdict(payments, login, summary="Payments break 3.1.1."))
    review = await review_prompt(ctx, "Sell premium themes via Stripe")
    assert review.verdict == "violation" and review.summary == "Payments break 3.1.1."
    worst = review.findings[0]
    assert (worst.guideline, worst.fix) == ("3.1.1", "sell them with in-app purchase")


async def test_areas_left_out_count_as_concerns(tmp_project: Path) -> None:
    body = {"summary": "ok", "findings": [{"area": "safety", "status": "ok", "reason": "fine"}]}
    ctx, _ = reviewer_ctx(tmp_project, FakeTurn(text=json.dumps(body)))
    review = await review_prompt(ctx, "A tally counter")
    assert review.verdict == "concern"
    missing = [f for f in review.findings if f.area != "safety"]
    assert {f.area for f in missing} == set(AREAS) - {"safety"}
    assert all(f.status == "concern" and "not judged" in f.reason for f in missing)


async def test_a_plan_review_sees_the_steps(tmp_project: Path) -> None:
    ctx, fake = reviewer_ctx(tmp_project, verdict())
    spec = TaskSpec(goal="A tally counter", context="", requirements=["counts taps"],
                    acceptance_criteria=["the number goes up"], size="small")  # fmt: skip
    plan = Plan(spec=spec, steps=[
        Step(id="s1", title="Counter model", detail="struct Counter with increment",
             files=["Shared/Counter.swift"], check="review: tested"),
        Step(id="s2", title="Watch screen", detail="a big number and a button",
             depends_on=["s1"], check="review: looks right"),
    ])  # fmt: skip
    review = await review_plan(ctx, plan)
    assert review.stage == "plan"
    text = request_text(fake)
    assert "A tally counter" in text and "counts taps" in text
    assert "Counter model" in text and "a big number and a button" in text


async def test_a_product_review_looks_at_every_screenshot(tmp_project: Path) -> None:
    ctx, fake = reviewer_ctx(tmp_project, verdict())
    ctx.state.apple_screens = [
        AppleScreen(platform="ios", device="iPhone 16", dark=False, image=SHOT),
        AppleScreen(platform="watchos", device="Apple Watch Series 10 (46mm)", dark=True,
                    image=SHOT),
    ]  # fmt: skip
    review = await review_product(ctx, "build for ios: succeeded\ntests: 2 run, 0 failed")
    assert review.stage == "product"
    messages = fake.requests[0].messages
    images = [p for m in messages for p in m.parts if isinstance(p, ImagePart)]
    assert images == [SHOT, SHOT]
    text = request_text(fake)
    assert "iPhone 16 (ios, light mode)" in text and "(watchos, dark mode)" in text
    assert "tests: 2 run, 0 failed" in text


async def test_a_reviewer_that_cannot_see_is_told_so(tmp_project: Path) -> None:
    ctx, fake = reviewer_ctx(tmp_project, verdict(), vision=False)
    ctx.state.apple_screens = [AppleScreen(platform="ios", device="iPhone 16", dark=False,
                                           image=SHOT)]  # fmt: skip
    await review_product(ctx)
    messages: list[Message] = fake.requests[0].messages
    assert not any(isinstance(p, ImagePart) for m in messages for p in m.parts)
    assert "cannot see images" in request_text(fake)


async def test_an_unreadable_answer_gets_one_more_try(tmp_project: Path) -> None:
    ctx, fake = reviewer_ctx(tmp_project, FakeTurn(text="Looks fine to me!"), verdict())
    review = await review_prompt(ctx, "A tally counter")
    assert review.verdict == "ok" and not review.error
    assert "could not be used" in request_text(fake, 1)


async def test_a_review_that_fails_is_an_error_not_a_pass(tmp_project: Path) -> None:
    ctx, _ = reviewer_ctx(tmp_project, FakeTurn(text="no"), FakeTurn(text="still no"))
    review = await review_prompt(ctx, "A tally counter")
    assert review.error and review.verdict == "concern" and not review.findings
    broken, _ = reviewer_ctx(tmp_project, FakeTurn(error="auth"))
    failed = await review_prompt(broken, "A tally counter")
    assert failed.error and failed.verdict == "concern"


def test_the_review_event_round_trips_and_renders() -> None:
    finding = GuidelineFinding(area="legal", status="violation", guideline="5.1.1",
                               reason="no privacy policy", fix="link one in the app")  # fmt: skip
    review = GuidelineReview(session_id="s", ts=1.0, stage="product", verdict="violation",
                             summary="Privacy is missing.", findings=[finding])  # fmt: skip
    assert parse_event(review.model_dump_json()) == review
    shown = describe(review).plain
    assert "product" in shown and "violation" in shown and "Privacy is missing." in shown
    assert "5.1.1" in shown and "link one in the app" in shown
