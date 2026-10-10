"""Hosting checkpoints in the pipeline (S67b, `--app` or `[app] review`): a full-stack product
is checked before it may go live.

The release reviewer judges the request before planning; the architect's blueprint (S68) shapes
the plan, and the reviewer judges the plan with it before building. At the
end the product must pass its fixed checks (`forge app check`); only then is it run, photographed
at desktop and phone size, and reviewed. Failed checks and violations go back to the coder first;
what it cannot fix, the user decides. Last, the user says whether it may go live. Only their
"go live" counts: headless runs and `--yes` take the default, which is "not yet".
"""

import asyncio
import base64
import json
import time
from dataclasses import replace
from pathlib import Path
from typing import Any

from forge import app_dev, prompts
from forge.agent import run_agent
from forge.app_checks import AppCheckReport, check_app, report_text, save_report
from forge.blueprint import Blueprint, blueprint_for, blueprint_text
from forge.checks import sandbox_policy
from forge.ctx import Ctx
from forge.events import ReleaseReview
from forge.plan import Plan, Question, TaskSpec
from forge.ports import BrowserError
from forge.questions import ask
from forge.release_flow import CheckpointStopped, Outcome, Replan
from forge.release_review import Screen, review_plan, review_product, review_prompt

GO_LIVE = "Ready to go live"
SEND_BACK = "Send it back to the agent"
NOT_YET = "Not yet"
GO_ON = "Go on anyway"
STOP = "Stop here"
FIX_ROUNDS = 2  # the agent's own attempts before the user decides
SEND_BACKS = 3  # rounds of the user's feedback
OUT = Path(".forge") / "out" / "app"
SIZES = {"desktop": (1280, 800), "phone": (390, 844)}


class AppStopped(CheckpointStopped):
    """The run stops at a hosting checkpoint."""


class AppCheckpoint:
    """The release review of a full-stack product and the user's go-live decision."""

    name = "app"

    def __init__(self) -> None:
        self.blueprint: Blueprint | None = None

    async def prepare_plan(self, ctx: Ctx, spec: TaskSpec) -> TaskSpec:
        """The architect's blueprint (S68); the planner follows it."""
        spec, self.blueprint = await blueprint_for(ctx, spec)
        return spec

    async def check_request(self, ctx: Ctx, prompt: str) -> None:
        """Review the request against the release rulebooks."""
        await settle(ctx, await review_prompt(ctx, prompt))

    async def check_plan(self, ctx: Ctx, plan: Plan, replan: Replan) -> Plan:
        """Review the plan; a violation is planned again with the reviewer's fixes."""
        for attempt in range(FIX_ROUNDS + 1):
            review = await review_plan(ctx, plan, blueprint_text(self.blueprint))
            if not blocking(review) or review.error or attempt == FIX_ROUNDS:
                await settle(ctx, review)
                return plan
            constraints = [*plan.spec.constraints, *fixes(review)]
            plan = await replan(plan.spec.model_copy(update={"constraints": constraints}))
        return plan

    async def finish(self, ctx: Ctx) -> Outcome:
        """Check, photograph and review the product, then ask whether it may go live."""
        for _ in range(SEND_BACKS + 1):
            outcome = await checked_product(ctx)
            if outcome is not None:
                return outcome
            feedback = await ask_feedback(ctx)
            if not feedback:
                break
            await work_on(ctx, prompts.render("app_feedback", material=feedback))
        return Outcome(False, "You have not said that the product may go live.")

    def report_fields(self, outcome: Outcome) -> dict[str, Any]:
        """`ready_to_host` and `host_summary`."""
        return {"ready_to_host": outcome.ready, "host_summary": outcome.summary}


async def checked_product(ctx: Ctx) -> Outcome | None:
    """Get the checks green and the review clean, then ask (None: the user sent it back)."""
    review: ReleaseReview | None = None
    for attempt in range(FIX_ROUNDS + 1):
        checks = await run_checks(ctx)
        review = None
        if checks.ok:
            review = await review_product(ctx, report_text(checks), await take_screens(ctx))
            if not blocking(review) or review.error:
                break
        if attempt == FIX_ROUNDS:
            break
        await work_on(ctx, prompts.render("app_fix", material=problems(checks, review)))
    if review is None:
        return Outcome(False, f"The product does not pass its checks:\n{report_text(checks)}")
    if blocking(review) and not await user_goes_on(ctx, review):
        return Outcome(False, stop_reason(review))
    choice = await ask_go_live(ctx, review, checks)
    if choice == GO_LIVE:
        save_approval(ctx.root, checks, review)
        commit = checks.commit[:12] or "the working tree"
        note = " (it has uncommitted changes: commit them first)" if checks.dirty else ""
        return Outcome(True, f"You said {commit} may go live{note}.")
    return None if choice == SEND_BACK else Outcome(False, "You have not said it may go live yet.")


async def run_checks(ctx: Ctx) -> AppCheckReport:
    """`forge app check` in the session's sandbox; the report is saved for the user."""
    report = await check_app(ctx.root, ctx.executor, sandbox_policy(ctx))
    save_report(ctx.root, report)
    return report


async def take_screens(ctx: Ctx) -> list[Screen]:
    """Run the product and photograph its start page at desktop and phone size."""
    factory = ctx.state.preview_browsers
    if factory is None:
        return []
    shown: list[str] = []
    async with app_dev.running_product(
        ctx.root, ctx.executor, sandbox_policy(ctx), shown.append
    ) as product:
        if product is None or not await app_dev.wait_healthy(product.manifest):
            return []
        url = app_dev.app_url(product.manifest)
        screens: list[Screen] = []
        for name, size in SIZES.items():
            try:
                browser = await factory.new_browser(viewport=size)
                try:
                    view = await browser.open(url)
                finally:
                    await browser.close()
            except BrowserError:
                continue
            if view.image is not None:
                screens.append(Screen(f"{name} ({size[0]}x{size[1]}), {url}", view.image))
                await save_screen(ctx.root, name, view.image.data_b64)
        return screens


async def save_screen(root: Path, name: str, data_b64: str) -> None:
    """Save a screenshot under .forge/out/app/ for the user to look at."""
    path = root / OUT / f"{name}.png"
    data = base64.b64decode(data_b64)

    def write() -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)

    await asyncio.to_thread(write)


def save_approval(root: Path, checks: AppCheckReport, review: ReleaseReview) -> None:
    """Record what the user approved: the checked commit, its checks and the review."""
    body = {
        "commit": checks.commit,
        "dirty": checks.dirty,
        "approved_at": time.time(),
        "checks_ok": checks.ok,
        "review_verdict": review.verdict,
        "review_summary": review.summary,
    }
    path = root / OUT / "approval.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(body, indent=2) + "\n", encoding="utf-8")


def blocking(review: ReleaseReview) -> bool:
    """A violation, or a review that could not be made, needs more than a note."""
    return review.verdict == "violation" or bool(review.error)


def fixes(review: ReleaseReview) -> list[str]:
    """The reviewer's violations, as constraints for the next plan."""
    return [
        f"Release review ({f.rule or f.area}): {f.fix or f.reason}"
        for f in review.findings
        if f.status == "violation"
    ]


def problems(checks: AppCheckReport, review: ReleaseReview | None) -> str:
    """What the coder has to fix: the failed checks, or the reviewer's violations."""
    if review is None:
        return report_text(checks)
    return "\n".join(
        f"- {f.rule or f.area}: {f.reason}. Fix: {f.fix or 'see the rule'}"
        for f in review.findings
        if f.status == "violation"
    )


def stop_reason(review: ReleaseReview) -> str:
    """Why the run stopped at a review."""
    return f"Stopped at the release review of the {review.stage}: {review.summary}"


def findings_text(review: ReleaseReview) -> str:
    """The findings that are not ok, one per line."""
    lines = [
        f"- {f.status} ({f.rule or f.area}): {f.reason}" + (f" Fix: {f.fix}" if f.fix else "")
        for f in review.findings
        if f.status != "ok"
    ]
    return "\n".join(lines) or "- no problems found"


async def settle(ctx: Ctx, review: ReleaseReview) -> None:
    """A blocking review needs the user's go-ahead; without it the run stops."""
    if blocking(review) and not await user_goes_on(ctx, review):
        raise AppStopped(stop_reason(review))


async def user_goes_on(ctx: Ctx, review: ReleaseReview) -> bool:
    """Ask whether to go on despite the review (the default is to stop)."""
    question = Question(
        text=f"The release review of the {review.stage} found problems: {review.summary}\n"
        + findings_text(review),
        kind="choice",
        options=[STOP, GO_ON],
        default=STOP,
        why="The product must not go live like this.",
    )
    answers = await ask(ctx, [question])
    if not answers or answers[0] != GO_ON:
        return False
    note = f"You let the {review.stage} go on despite the release review: {review.summary}"
    ctx.state.notes.append(note)
    return True


async def ask_go_live(ctx: Ctx, review: ReleaseReview, checks: AppCheckReport) -> str:
    """The user's decision on the finished product (the default is not yet)."""
    commit = checks.commit[:12] or "the working tree"
    question = Question(
        text=f"May the product go live? Its checks passed on {commit}. The release review: "
        f"{review.summary}\n{findings_text(review)}\nScreenshots and the checks are in "
        f"{OUT.as_posix()}.",
        kind="choice",
        options=[NOT_YET, GO_LIVE, SEND_BACK],
        default=NOT_YET,
        why="Only a product you approve is offered for hosting; the operator approves it too.",
    )
    answers = await ask(ctx, [question])
    return answers[0] if answers else NOT_YET


async def ask_feedback(ctx: Ctx) -> str:
    """What the user wants changed before they look again."""
    question = Question(
        text="What should the agent change?",
        kind="text",
        default="",
        why="The agent works on it; then the product is checked, reviewed and shown again.",
    )
    answers = await ask(ctx, [question])
    return answers[0].strip() if answers else ""


async def work_on(ctx: Ctx, task: str) -> None:
    """Let the coder work on problems found after the plan was done."""
    coder = replace(ctx, role="coder")
    await run_agent(coder, task, role="coder", max_turns=ctx.cfg.limits.max_turns_per_step)
