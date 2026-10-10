"""The App Store listing (S61): drafted after the user approves the app, within Apple's limits,
checked by the independent Apple reviewer, and saved for the user to finish."""

import json
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from forge.apple_flow import APPROVE
from forge.apple_listing import LISTING, StoreListing
from forge.pipeline import run_task
from forge.providers.fake import FakeTurn
from test_apple_pipeline import apple_run, requests_of, review, reviews

DRAFT: dict[str, Any] = {
    "locale": "en-US",
    "name": "Tally",
    "subtitle": "Count anything with one tap",
    "description": "Tally counts whatever you want: tap to add one, reset when you are done.",
    "keywords": "counter,tally,count,clicker",
    "promotional_text": "",
    "copyright": "2026 Ada Lovelace",
    "primary_category": "UTILITIES",
    "age_rating": {},
    "collects_data": False,
    "privacy": [],
    "notes": ["Add a support URL and a privacy policy URL."],
}


def draft(**changes: Any) -> FakeTurn:
    """The writer's answer: the listing as JSON."""
    return FakeTurn(text="```json\n" + json.dumps({**DRAFT, **changes}) + "\n```")


def test_the_listing_keeps_apples_limits() -> None:
    assert StoreListing.model_validate(DRAFT).name == "Tally"
    for wrong in ({"name": "T"}, {"name": "x" * 31}, {"subtitle": "y" * 31},
                  {"keywords": "ä" * 51},  # 102 bytes: Apple counts bytes, not letters
                  {"promotional_text": "z" * 171}, {"description": ""},
                  {"support_url": "http://example.com"}, {"locale": "english"},
                  {"primary_category": "TOYS"}):  # fmt: skip
        with pytest.raises(ValidationError):
            StoreListing.model_validate({**DRAFT, **wrong})
    fine = StoreListing.model_validate({**DRAFT, "support_url": "https://example.com/help"})
    assert fine.age_rating.violence_realistic == "NONE" and not fine.age_rating.gambling


async def test_after_the_approval_forge_drafts_the_listing_and_has_it_reviewed(
    tmp_project: Path,
) -> None:
    sneaky = draft(support_url="https://made-up.example/support")  # URLs are the user's
    ctx, fake = apple_run(tmp_project, [review(), review(), review(), review(summary="Fine.")],
                          answers=[[APPROVE]], listing=[sneaky])  # fmt: skip
    report = await run_task("a tally counter", ctx)
    assert report.ok and report.ready_for_apple
    assert [r.stage for r in reviews(ctx)] == ["prompt", "plan", "product", "listing"]
    saved = StoreListing.model_validate_json((tmp_project / LISTING).read_text())
    assert saved.name == "Tally" and saved.support_url == ""  # never invented
    assert "store texts" in report.apple_summary
    review_request = requests_of(fake, "apple_reviewer")[-1]
    assert "Count anything with one tap" in review_request


async def test_a_listing_that_breaks_the_rules_is_drafted_once_more_with_the_fixes(
    tmp_project: Path,
) -> None:
    flagged = review("violation", fix="do not promise features the app lacks")
    ctx, fake = apple_run(tmp_project, [review(), review(), review(), flagged, review()],
                          answers=[[APPROVE]],
                          listing=[draft(description="The best counter ever, with cloud sync."),
                                   draft()])  # fmt: skip
    report = await run_task("a tally counter", ctx)
    assert report.ready_for_apple
    assert [r.stage for r in reviews(ctx)][-2:] == ["listing", "listing"]
    second = requests_of(fake, "apple_writer")[-1]
    assert "do not promise features the app lacks" in second
    saved = StoreListing.model_validate_json((tmp_project / LISTING).read_text())
    assert "cloud" not in saved.description


async def test_without_a_draft_the_app_stays_approved(tmp_project: Path) -> None:
    garbage = [FakeTurn(text="no idea"), FakeTurn(text="still no JSON")]
    ctx, _ = apple_run(tmp_project, [review(), review(), review()], answers=[[APPROVE]],
                       listing=garbage)  # fmt: skip
    report = await run_task("a tally counter", ctx)
    assert report.ready_for_apple
    assert not (tmp_project / LISTING).exists()
    assert "could not be drafted" in report.apple_summary


async def test_without_the_listing_option_nothing_is_drafted(tmp_project: Path) -> None:
    ctx, fake = apple_run(tmp_project, [review(), review(), review()], answers=[[APPROVE]])
    report = await run_task("a tally counter", ctx)
    assert report.ready_for_apple and not requests_of(fake, "apple_writer")
    assert not (tmp_project / LISTING).exists()
