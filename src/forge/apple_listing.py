"""The App Store listing (S61): what the App Store shows about an Apple app, drafted by Forge
after the user approves the app and checked by the independent Apple reviewer.

The listing is saved in `.forge/out/apple/listing.json` for the user (or a server) to finish and
send to Apple. Its limits are Apple's; the support, marketing and privacy policy URLs are always
left for the user, never invented.
"""

import asyncio
from dataclasses import replace
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, field_validator

from forge import prompts
from forge.agent import run_agent
from forge.apple_review import review
from forge.ctx import Ctx
from forge.events import GuidelineReview
from forge.runtime.ledger import ReadLedger
from forge.structured import StructuredError, parse_as

ROLE = "apple_writer"
LISTING = Path(".forge") / "out" / "apple" / "listing.json"
WRITE_TURNS = 20  # reading the project
FIX_TURNS = 3
DRAFTS = 2  # a listing the reviewer rejects is drafted once more with the fixes
URL = r"^$|^https://\S{1,1990}$"
Frequency = Literal["NONE", "INFREQUENT", "FREQUENT"]
Category = Literal[
    "BOOKS", "BUSINESS", "DEVELOPER_TOOLS", "EDUCATION", "ENTERTAINMENT", "FINANCE",
    "FOOD_AND_DRINK", "GAMES", "GRAPHICS_AND_DESIGN", "HEALTH_AND_FITNESS", "LIFESTYLE",
    "MEDICAL", "MUSIC", "NAVIGATION", "NEWS", "PHOTO_AND_VIDEO", "PRODUCTIVITY", "REFERENCE",
    "SHOPPING", "SOCIAL_NETWORKING", "SPORTS", "TRAVEL", "UTILITIES", "WEATHER",
]  # fmt: skip


class AgeRating(BaseModel):
    """Answers to App Store Connect's age rating questions."""

    alcohol_tobacco_or_drug_use_or_references: Frequency = "NONE"
    contests: Frequency = "NONE"
    gambling_simulated: Frequency = "NONE"
    guns_or_other_weapons: Frequency = "NONE"
    horror_or_fear_themes: Frequency = "NONE"
    mature_or_suggestive_themes: Frequency = "NONE"
    medical_or_treatment_information: Frequency = "NONE"
    profanity_or_crude_humor: Frequency = "NONE"
    sexual_content_graphic_and_nudity: Frequency = "NONE"
    sexual_content_or_nudity: Frequency = "NONE"
    violence_cartoon_or_fantasy: Frequency = "NONE"
    violence_realistic: Frequency = "NONE"
    violence_realistic_prolonged_graphic_or_sadistic: Frequency = "NONE"
    gambling: bool = False
    unrestricted_web_access: bool = False
    user_generated_content: bool = False
    messaging_and_chat: bool = False
    advertising: bool = False


class PrivacyAnswer(BaseModel):
    """One kind of data the app collects, as App Store Connect's privacy questions ask it."""

    data_type: str = Field(min_length=1, max_length=100)  # e.g. "Crash Data", "Email Address"
    purposes: list[str] = Field(default_factory=list, max_length=10)
    linked_to_user: bool = False
    used_for_tracking: bool = False


class ListingDraft(BaseModel):
    """What the writer drafts: everything but the URLs, within Apple's limits."""

    locale: str = Field(default="en-US", pattern=r"^[a-z]{2,3}(-[A-Za-z]{2,4})?$")
    name: str = Field(min_length=2, max_length=30)
    subtitle: str = Field(default="", max_length=30)
    description: str = Field(min_length=1, max_length=4000)
    keywords: str = ""  # comma-separated, at most 100 bytes
    promotional_text: str = Field(default="", max_length=170)
    whats_new: str = Field(default="", max_length=4000)  # only for versions after the first
    copyright: str = Field(default="", max_length=200)
    primary_category: Category
    secondary_category: Category | None = None
    age_rating: AgeRating = Field(default_factory=AgeRating)
    collects_data: bool = False
    privacy: list[PrivacyAnswer] = Field(default_factory=list, max_length=40)
    notes: list[str] = Field(default_factory=list, max_length=20)  # for the user to check

    @field_validator("keywords")
    @classmethod
    def keywords_fit(cls, value: str) -> str:
        """Apple counts the keywords' bytes, not their letters."""
        if len(value.encode("utf-8")) > 100:
            raise ValueError("the keywords may take at most 100 bytes")
        return value


class StoreListing(ListingDraft):
    """The listing as the App Store shows it; the user adds the URLs."""

    support_url: str = Field(default="", pattern=URL)
    marketing_url: str = Field(default="", pattern=URL)
    privacy_policy_url: str = Field(default="", pattern=URL)


async def prepare_listing(ctx: Ctx) -> str:
    """Draft the listing, have it reviewed, save it; one sentence for the user's summary."""
    found = await reviewed_listing(ctx)
    if found is None:
        return "The store texts could not be drafted; ask Forge for them again."
    listing, verdict = found
    await save_listing(ctx.root, listing)
    return (f"The store texts are drafted in {LISTING.as_posix()} (Apple review: "
            f"{verdict.verdict}); add the support and privacy policy URLs.")  # fmt: skip


async def reviewed_listing(ctx: Ctx) -> tuple[StoreListing, GuidelineReview] | None:
    """A draft and its review; a rejected draft is drafted once more with the fixes."""
    fixes: list[str] = []
    for attempt in range(DRAFTS):
        listing = await draft_listing(ctx, fixes)
        if listing is None:
            return None
        task = prompts.render("apple_review_listing", material=listing.model_dump_json(indent=2))
        verdict = await review(ctx, "listing", task, ctx.state.apple_screens)
        if verdict.verdict != "violation" or verdict.error or attempt == DRAFTS - 1:
            return listing, verdict
        fixes = violations(verdict)
    return None


async def draft_listing(ctx: Ctx, fixes: list[str]) -> StoreListing | None:
    """The writer's draft, with the URLs left empty; None when no usable draft came back."""
    writer = replace(ctx, agent_id="apple-writer", role=ROLE, ledger=ReadLedger())
    task = prompts.render("apple_listing", material=material(ctx, fixes))
    result = await run_agent(writer, task, role=ROLE, max_turns=WRITE_TURNS)
    if result.stopped == "error":
        return None
    try:
        draft = parse_as(result.text, ListingDraft)
    except StructuredError as problem:
        retry = prompts.render("fix_json", failure=str(problem))
        again = await run_agent(writer, retry, role=ROLE, history=result.messages,
                                max_turns=FIX_TURNS)  # fmt: skip
        try:
            draft = parse_as(again.text, ListingDraft)
        except StructuredError:
            return None
    return StoreListing(**draft.model_dump())


def material(ctx: Ctx, fixes: list[str]) -> str:
    """What the writer is told besides the project: the review of the app, and what to fix."""
    products = [r for r in ctx.state.apple_reviews if r.stage == "product"]
    lines = [f"The Apple review of the app: {products[-1].summary}"] if products else []
    if fixes:
        lines += ["The Apple reviewer rejected the last draft. Fix this:", *fixes]
    return "\n".join(lines) or "(nothing more)"


def violations(verdict: GuidelineReview) -> list[str]:
    """The reviewer's violations, as instructions for the next draft."""
    return [f"- {f.guideline or f.area}: {f.fix or f.reason}"
            for f in verdict.findings if f.status == "violation"]  # fmt: skip


async def save_listing(root: Path, listing: StoreListing) -> None:
    """Write the listing where the user and a server find it."""
    path = root / LISTING

    def write() -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(listing.model_dump_json(indent=2), encoding="utf-8")

    await asyncio.to_thread(write)
