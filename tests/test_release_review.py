"""The independent release reviewer (S66): a fresh context, read-only tools, a verdict per
rulebook (built-in and the operator's own); a failed review never passes."""

import json
from pathlib import Path
from typing import Any

import pytest

from forge.config import ForgeConfig, ReleaseConfig, load_config
from forge.ctx import Ctx
from forge.events import ReleaseFinding, ReleaseReview, parse_event
from forge.local.rich_renderer import describe
from forge.plan import Plan, Step, TaskSpec
from forge.providers.base import Capabilities, ImagePart, text_message
from forge.providers.fake import FakeProvider, FakeTurn
from forge.release_review import (
    RELEASE_AREAS,
    ReviewFailed,
    Screen,
    load_rulebooks,
    review_plan,
    review_product,
    review_prompt,
)
from forge.wiring import install_fake
from support import drain, make_ctx

SHOT = ImagePart(media_type="image/png", data_b64="iVBORw0KGgo=")


def verdict(
    *findings: dict[str, Any], areas: tuple[str, ...] = RELEASE_AREAS, summary: str = "Fine."
) -> FakeTurn:
    """A reviewer answer: the given findings, every other area ok."""
    named = {f["area"] for f in findings}
    ok = [{"area": a, "status": "ok", "reason": "kept"} for a in areas if a not in named]
    body = {"summary": summary, "findings": [*findings, *ok]}
    return FakeTurn(text=f"```json\n{json.dumps(body)}\n```")


def reviewer_ctx(
    root: Path, *turns: FakeTurn, vision: bool = True, cfg: ForgeConfig | None = None
) -> tuple[Ctx, FakeProvider]:
    ctx = make_ctx(root, cfg=cfg)
    caps = Capabilities(context_window=128_000, max_output=8_192, vision=vision)
    fake = install_fake(ctx.cfg, FakeProvider(roles={"release_reviewer": list(turns)}, caps=caps))
    return ctx, fake


def request_text(fake: FakeProvider, index: int = 0) -> str:
    return "\n".join(m.text() for m in fake.requests[index].messages)


async def test_a_clean_request_passes_every_rulebook(tmp_project: Path) -> None:
    ctx, fake = reviewer_ctx(tmp_project, verdict())
    events = ctx.bus.subscribe("*")
    review = await review_prompt(ctx, "A shared shopping list for families")
    assert review.stage == "prompt" and review.verdict == "ok" and not review.error
    assert {f.area for f in review.findings} == set(RELEASE_AREAS) == set(review.areas)
    request = fake.requests[0]
    assert request.model == "release_reviewer"
    assert "A shared shopping list" in request_text(fake)
    for rule in ("DSA Art. 16", "DDG § 5", "BGB § 312j", "GDPR", "acceptable use"):
        assert rule in request.system
    published = [e for e in await drain(events) if isinstance(e, ReleaseReview)]
    assert published == [review] == ctx.state.release_reviews


async def test_user_content_without_reporting_is_a_violation(tmp_project: Path) -> None:
    dsa = {
        "area": "content",
        "status": "violation",
        "rule": "DSA Art. 16",
        "reason": "posts are public but nobody can report them",
        "fix": "add the report button and the moderation queue",
    }
    impressum = {
        "area": "german_law",
        "status": "violation",
        "rule": "DDG § 5",
        "reason": "no page links the Impressum",
        "fix": "link /impressum in the footer",
    }
    ctx, _ = reviewer_ctx(tmp_project, verdict(dsa, impressum, summary="Two blockers."))
    review = await review_product(ctx, "app check: passed")
    assert review.verdict == "violation" and review.summary == "Two blockers."
    assert [f.area for f in review.findings[:2]] == ["content", "german_law"]
    text = describe(review).plain  # type: ignore[union-attr]
    assert "Release review of the product: violation" in text
    assert "content DSA Art. 16: posts are public" in text
    assert "4 areas ok" in text


async def test_the_reviewer_sees_nothing_of_the_builders_work(tmp_project: Path) -> None:
    ctx, fake = reviewer_ctx(tmp_project, verdict())
    ctx.session.messages += [
        text_message("user", "build it"),
        text_message("assistant", "I will skip the cookie banner"),
    ]
    await review_prompt(ctx, "A recipe site")
    assert "cookie banner" not in request_text(fake) and "build it" not in request_text(fake)


async def test_the_reviewer_may_read_and_fetch_but_never_change(tmp_project: Path) -> None:
    ctx, fake = reviewer_ctx(tmp_project, verdict())
    (tmp_project / "forge.app.toml").write_text('name = "x"\n', encoding="utf-8")
    await review_prompt(ctx, "A recipe site")
    tools = {t.name for t in fake.requests[0].tools}
    assert {"read_file", "grep", "list_dir", "web_fetch"} <= tools
    changing = {"write_file", "edit_file", "bash", "app_check", "ask_user", "spawn_agent"}
    assert not tools & changing


async def test_an_operators_rulebook_adds_an_area(tmp_project: Path, tmp_path: Path) -> None:
    book = tmp_path / "Payments Policy.md"
    book.write_text("- No sales of tobacco or alcohol.\n", encoding="utf-8")
    cfg = ForgeConfig(release=ReleaseConfig(rulebook_files=[str(book)]))
    tobacco = {
        "area": "payments_policy",
        "status": "violation",
        "rule": "no tobacco",
        "reason": "the shop sells cigars",
        "fix": "remove the tobacco products",
    }
    areas = (*RELEASE_AREAS, "payments_policy")
    ctx, fake = reviewer_ctx(tmp_project, verdict(tobacco, areas=areas), cfg=cfg)
    review = await review_prompt(ctx, "A shop for cigars")
    assert review.areas == [*RELEASE_AREAS, "payments_policy"]
    assert review.verdict == "violation" and review.findings[0].area == "payments_policy"
    assert '<rulebook name="payments_policy">' in request_text(fake)
    assert "No sales of tobacco" in request_text(fake)


async def test_an_unjudged_area_is_a_concern(tmp_project: Path, tmp_path: Path) -> None:
    book = tmp_path / "privacy.md"  # named like a built-in rulebook: kept apart
    book.write_text("- Keep logs 7 days at most.\n", encoding="utf-8")
    cfg = ForgeConfig(release=ReleaseConfig(rulebook_files=[str(book)]))
    ctx, _ = reviewer_ctx(tmp_project, verdict(), cfg=cfg)
    review = await review_prompt(ctx, "A notes app")
    assert review.verdict == "concern"
    missing = [f for f in review.findings if f.status == "concern"]
    assert [f.area for f in missing] == ["operator_privacy"]


async def test_a_missing_rulebook_fails_the_review(tmp_project: Path, tmp_path: Path) -> None:
    cfg = ForgeConfig(release=ReleaseConfig(rulebook_files=[str(tmp_path / "gone.md")]))
    ctx, fake = reviewer_ctx(tmp_project, verdict(), cfg=cfg)
    review = await review_prompt(ctx, "A notes app")
    assert review.verdict == "concern" and "gone.md" in review.error
    assert fake.requests == []  # no model asked without every rulebook


async def test_malformed_json_is_asked_again_then_fails(tmp_project: Path) -> None:
    ctx, fake = reviewer_ctx(
        tmp_project, FakeTurn(text="looks fine to me"), FakeTurn(text="still fine")
    )
    review = await review_prompt(ctx, "A notes app")
    assert review.verdict == "concern" and review.error and review.findings == []
    assert len(fake.requests) == 2  # asked once more for the JSON


async def test_a_model_error_never_passes(tmp_project: Path) -> None:
    ctx, _ = reviewer_ctx(tmp_project, FakeTurn(error="overloaded"))
    review = await review_prompt(ctx, "A notes app")
    assert review.verdict == "concern"
    assert review.error


async def test_the_plan_and_the_product_are_reviewed_with_their_material(
    tmp_project: Path,
) -> None:
    ctx, fake = reviewer_ctx(tmp_project, verdict(), verdict())
    spec = TaskSpec(
        goal="A recipe site with comments",
        context="",
        requirements=["comments"],
        acceptance_criteria=["comments can be reported"],
        size="medium",
    )
    step = Step(id="s1", title="Comments", detail="report button", check="pytest")
    plan = Plan(spec=spec, steps=[step])
    assert (await review_plan(ctx, plan)).stage == "plan"
    assert "report button" in request_text(fake, 0)
    shots = [Screen("desktop, light mode", SHOT), Screen("phone, dark mode", SHOT)]
    review = await review_product(ctx, "app check: passed\nPASSED web", shots)
    assert review.stage == "product"
    product = fake.requests[1]
    assert "app check: passed" in request_text(fake, 1)
    images = [p for m in product.messages for p in m.parts if isinstance(p, ImagePart)]
    assert len(images) == 2
    assert "2. phone, dark mode" in request_text(fake, 1)


async def test_a_blind_model_is_told_the_screenshots_are_missing(tmp_project: Path) -> None:
    ctx, fake = reviewer_ctx(tmp_project, verdict(), vision=False)
    await review_product(ctx, "app check: passed", [Screen("desktop", SHOT)])
    request = fake.requests[0]
    assert not [p for m in request.messages for p in m.parts if isinstance(p, ImagePart)]
    assert "cannot see images" in request_text(fake)


def test_the_review_is_one_json_line() -> None:
    finding = ReleaseFinding(area="content", status="violation", rule="DSA Art. 16", reason="x")
    review = ReleaseReview(
        session_id="s",
        ts=1.0,
        stage="product",
        verdict="violation",
        summary="no",
        findings=[finding],
        areas=list(RELEASE_AREAS),
    )
    line = review.model_dump_json()
    assert "\n" not in line and parse_event(line) == review


def test_rulebook_paths_start_at_forge_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FORGE_HOME", str(tmp_path))
    (tmp_path / "rules").mkdir()
    (tmp_path / "rules" / "Shop rules.md").write_text("x" * 30_000, encoding="utf-8")
    books = load_rulebooks(
        ForgeConfig(release=ReleaseConfig(rulebook_files=["rules/Shop rules.md"]))
    )
    assert [b.name for b in books] == ["shop_rules"]
    assert len(books[0].text) == 20_000
    with pytest.raises(ReviewFailed):
        load_rulebooks(ForgeConfig(release=ReleaseConfig(rulebook_files=["missing.md"])))


def test_an_untrusted_project_cannot_change_the_rulebooks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FORGE_HOME", str(tmp_path / "home"))
    project = tmp_path / "project"
    (project / ".forge").mkdir(parents=True)
    (project / ".forge" / "config.toml").write_text(
        '[release]\nrulebook_files = ["lenient.md"]\n', encoding="utf-8"
    )
    cfg = load_config(project)
    assert cfg.release.rulebook_files == []
    assert any("[release]" in warning for warning in cfg.warnings)
