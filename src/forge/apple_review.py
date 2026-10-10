"""The independent Apple reviewer (S59): the request, the plan or the finished app, judged
against Apple's App Store Review Guidelines and Human Interface Guidelines.

The reviewer is an agent of its own role (and so its own model). It starts from a fresh context
that holds only what it is asked to judge, may read the project and fetch Apple's pages, and
never changes anything. It answers with findings per guideline area; the worst finding is the
verdict, and a review that could not be made is never a pass.
"""

import time
from dataclasses import replace
from typing import Literal, get_args

from pydantic import BaseModel

from forge import prompts
from forge.agent import run_agent
from forge.ctx import Ctx
from forge.events import GuidelineArea, GuidelineFinding, GuidelineReview, GuidelineStatus
from forge.plan import Plan
from forge.ports import AppleScreen
from forge.providers.base import Message, ProviderError, TextPart, text_message
from forge.providers.registry import resolve_role
from forge.runtime.ledger import ReadLedger
from forge.structured import StructuredError, parse_as

ROLE = "apple_reviewer"
AREAS: tuple[GuidelineArea, ...] = get_args(GuidelineArea)
SEVERITY: dict[GuidelineStatus, int] = {"ok": 0, "concern": 1, "violation": 2}
REVIEW_TURNS = 30  # reading the project and a few of Apple's pages
FIX_TURNS = 3
Stage = Literal["prompt", "plan", "product", "listing"]


class ReviewAnswer(BaseModel):
    """The reviewer's JSON answer."""

    summary: str
    findings: list[GuidelineFinding]
    sources: list[str] = []


class ReviewFailed(Exception):
    """The reviewer's model could not be reached."""


async def review_prompt(ctx: Ctx, prompt: str) -> GuidelineReview:
    """Judge a request for an Apple app before anything is planned."""
    return await review(ctx, "prompt", prompts.render("apple_review_prompt", material=prompt))


async def review_plan(ctx: Ctx, plan: Plan) -> GuidelineReview:
    """Judge the task specification and the plan before anything is built."""
    return await review(ctx, "plan", prompts.render("apple_review_plan", material=plan_text(plan)))


async def review_product(ctx: Ctx, builds: str = "") -> GuidelineReview:
    """Judge the finished app: its project and code, and the latest screenshot of each device."""
    task = prompts.render("apple_review_product", material=builds or "(none)")
    return await review(ctx, "product", task, ctx.state.apple_screens)


async def review(
    ctx: Ctx, stage: Stage, task: str, screens: list[AppleScreen] | None = None
) -> GuidelineReview:
    """Run the reviewer on one stage and publish its verdict."""
    reviewer = replace(ctx, agent_id=f"apple-reviewer-{stage}", role=ROLE, ledger=ReadLedger())
    history = screens_message(reviewer, screens) if screens is not None else None
    try:
        answer = await ask(reviewer, task, history)
    except (ReviewFailed, StructuredError) as err:
        result = GuidelineReview(
            session_id=ctx.session.id, agent_id=reviewer.agent_id, ts=time.time(), stage=stage,
            verdict="concern", summary=f"The Apple review could not be made: {err}",
            error=str(err),
        )  # fmt: skip
    else:
        findings = all_areas(answer.findings)
        result = GuidelineReview(
            session_id=ctx.session.id, agent_id=reviewer.agent_id, ts=time.time(), stage=stage,
            verdict=max((f.status for f in findings), key=SEVERITY.__getitem__),
            summary=answer.summary, findings=findings, sources=answer.sources,
        )  # fmt: skip
    ctx.state.apple_reviews.append(result)
    await ctx.bus.publish(result)
    return result


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


def all_areas(findings: list[GuidelineFinding]) -> list[GuidelineFinding]:
    """The findings, worst first, plus a concern for each area the reviewer did not judge."""
    judged = {f.area for f in findings}
    missing = [
        GuidelineFinding(area=area, status="concern", reason="not judged by the reviewer")
        for area in AREAS
        if area not in judged
    ]
    return sorted([*findings, *missing], key=lambda f: -SEVERITY[f.status])


def screens_message(reviewer: Ctx, screens: list[AppleScreen]) -> list[Message]:
    """The screenshots as one message, or why the reviewer gets none."""
    if not screens:
        return [text_message("user", prompts.render("apple_review_no_screens"))]
    if not can_see(reviewer):
        return [text_message("user", prompts.render("apple_review_blind"))]
    labels = "\n".join(
        f"{number}. {s.device} ({s.platform}, {'dark' if s.dark else 'light'} mode)"
        for number, s in enumerate(screens, start=1)
    )
    intro = TextPart(text=prompts.render("apple_review_screens", material=labels))
    return [Message(role="user", parts=[intro, *(s.image for s in screens)])]


def can_see(reviewer: Ctx) -> bool:
    """True when the reviewer's first model reads images."""
    try:
        chain = resolve_role(ROLE, reviewer.cfg)
    except ProviderError:
        return False
    return bool(chain) and chain[0][0].capabilities(chain[0][1]).vision


def plan_text(plan: Plan) -> str:
    """The task specification and every step with its details, as the reviewer reads them."""
    steps = []
    for step in plan.steps:
        files = f"\nfiles: {', '.join(step.files)}" if step.files else ""
        steps.append(f"{step.id} {step.title}\n{step.detail}{files}")
    return plan.spec.model_dump_json(indent=2) + "\n\n" + "\n\n".join(steps)
