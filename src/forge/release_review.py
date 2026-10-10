"""The independent release reviewer (S66): the request, the plan or the finished product, judged
against rulebooks for hosting it for the public: acceptable use, privacy (GDPR), German law,
user content (DSA), security and resources, plus the operator's own rulebooks.

Like the Apple reviewer, it is an agent of its own role (and model) that starts from a fresh
context, may read the project and fetch pages, and never changes anything. The worst finding is
the verdict, and a review that could not be made is never a pass. The verdict advises; fixed
checks and two people decide whether a product goes live.
"""

import re
import time
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Literal

from pydantic import BaseModel

from forge import prompts
from forge.agent import run_agent
from forge.apple_review import plan_text
from forge.config import ForgeConfig, forge_home
from forge.ctx import Ctx
from forge.events import GuidelineStatus, ReleaseFinding, ReleaseReview
from forge.plan import Plan
from forge.providers.base import ImagePart, Message, ProviderError, TextPart, text_message
from forge.providers.registry import resolve_role
from forge.runtime.ledger import ReadLedger
from forge.structured import StructuredError, parse_as

ROLE = "release_reviewer"
RELEASE_AREAS = ("hosting", "privacy", "german_law", "content", "security", "resources")
SEVERITY: dict[GuidelineStatus, int] = {"ok": 0, "concern": 1, "violation": 2}
REVIEW_TURNS = 30  # reading the project and a few pages
FIX_TURNS = 3
MAX_RULEBOOK_CHARS = 20_000
Stage = Literal["prompt", "plan", "product"]


class ReviewAnswer(BaseModel):
    """The reviewer's JSON answer."""

    summary: str
    findings: list[ReleaseFinding]


class ReviewFailed(Exception):
    """The review could not be made: no model answered, or a rulebook cannot be read."""


@dataclass(frozen=True)
class Rulebook:
    """One of the operator's own rulebooks."""

    name: str
    text: str


@dataclass(frozen=True)
class Screen:
    """A screenshot of the product with what it shows ("desktop, light mode")."""

    label: str
    image: ImagePart


def load_rulebooks(cfg: ForgeConfig) -> list[Rulebook]:
    """The operator's rulebooks from `[release] rulebook_files`; one that cannot be read fails."""
    books: list[Rulebook] = []
    for entry in cfg.release.rulebook_files:
        path = Path(entry).expanduser()
        path = path if path.is_absolute() else forge_home() / path
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as error:
            raise ReviewFailed(f"the rulebook {path} cannot be read: {error.strerror}") from error
        name = re.sub(r"[^a-z0-9]+", "_", path.stem.lower()).strip("_") or "rulebook"
        name = f"operator_{name}" if name in RELEASE_AREAS else name
        books.append(Rulebook(name, text[:MAX_RULEBOOK_CHARS]))
    return books


async def review_prompt(ctx: Ctx, prompt: str) -> ReleaseReview:
    """Judge a request for a product before anything is planned."""
    return await review(ctx, "prompt", prompts.render("release_review_prompt", material=prompt))


async def review_plan(ctx: Ctx, plan: Plan) -> ReleaseReview:
    """Judge the task specification and the plan before anything is built."""
    task = prompts.render("release_review_plan", material=plan_text(plan))
    return await review(ctx, "plan", task)


async def review_product(
    ctx: Ctx, checks: str, screens: list[Screen] | None = None
) -> ReleaseReview:
    """Judge the finished product: its code, its fixed checks and its screenshots."""
    task = prompts.render("release_review_product", material=checks or "(none)")
    return await review(ctx, "product", task, screens or [])


async def review(
    ctx: Ctx, stage: Stage, task: str, screens: list[Screen] | None = None
) -> ReleaseReview:
    """Run the reviewer on one stage and publish its verdict."""
    reviewer = replace(ctx, agent_id=f"release-reviewer-{stage}", role=ROLE, ledger=ReadLedger())
    areas = list(RELEASE_AREAS)
    try:
        books = load_rulebooks(ctx.cfg)
        areas += [book.name for book in books]
        history = screens_message(reviewer, screens)
        answer = await ask(reviewer, task + rulebooks_text(books), history)
    except (ReviewFailed, StructuredError) as err:
        result = ReleaseReview(
            session_id=ctx.session.id,
            agent_id=reviewer.agent_id,
            ts=time.time(),
            stage=stage,
            verdict="concern",
            summary=f"The release review could not be made: {err}",
            areas=areas,
            error=str(err),
        )
    else:
        findings = all_areas(answer.findings, areas)
        result = ReleaseReview(
            session_id=ctx.session.id,
            agent_id=reviewer.agent_id,
            ts=time.time(),
            stage=stage,
            verdict=max((f.status for f in findings), key=SEVERITY.__getitem__),
            summary=answer.summary,
            findings=findings,
            areas=areas,
        )
    ctx.state.release_reviews.append(result)
    await ctx.bus.publish(result)
    return result


def rulebooks_text(books: list[Rulebook]) -> str:
    """The operator's rulebooks for the review task (empty without any)."""
    if not books:
        return ""
    parts = [f'<rulebook name="{book.name}">\n{book.text.strip()}\n</rulebook>' for book in books]
    return "\n\n" + prompts.render("release_rulebooks", material="\n\n".join(parts))


async def ask(reviewer: Ctx, task: str, history: list[Message] | None) -> ReviewAnswer:
    """Run the reviewer; ask once more when its answer is not the JSON verdict."""
    result = await run_agent(reviewer, task, role=ROLE, history=history, max_turns=REVIEW_TURNS)
    if result.stopped == "error":
        raise ReviewFailed(result.text)
    try:
        return parse_as(result.text, ReviewAnswer)
    except StructuredError as problem:
        retry = prompts.render("fix_json", failure=str(problem))
        again = await run_agent(
            reviewer, retry, role=ROLE, history=result.messages, max_turns=FIX_TURNS
        )
        if again.stopped == "error":
            raise ReviewFailed(again.text) from problem
        return parse_as(again.text, ReviewAnswer)


def all_areas(findings: list[ReleaseFinding], areas: list[str]) -> list[ReleaseFinding]:
    """The findings, worst first, plus a concern for each area the reviewer did not judge."""
    judged = {f.area for f in findings}
    missing = [
        ReleaseFinding(area=area, status="concern", reason="not judged by the reviewer")
        for area in areas
        if area not in judged
    ]
    return sorted([*findings, *missing], key=lambda f: -SEVERITY[f.status])


def screens_message(reviewer: Ctx, screens: list[Screen] | None) -> list[Message] | None:
    """The screenshots as one message; a note when the reviewer's model cannot see them."""
    if not screens:
        return None
    if not can_see(reviewer):
        return [text_message("user", prompts.render("release_review_blind"))]
    labels = "\n".join(f"{n}. {s.label}" for n, s in enumerate(screens, start=1))
    intro = TextPart(text=prompts.render("release_review_screens", material=labels))
    return [Message(role="user", parts=[intro, *(s.image for s in screens)])]


def can_see(reviewer: Ctx) -> bool:
    """True when the reviewer's first model reads images."""
    try:
        chain = resolve_role(ROLE, reviewer.cfg)
    except ProviderError:
        return False
    return bool(chain) and chain[0][0].capabilities(chain[0][1]).vision
