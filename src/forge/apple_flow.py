"""Apple checkpoints in the pipeline (S60, `--apple` or `[apple] review`).

The request is reviewed before planning, the plan before building, and the app once it builds
and its tests pass, with a screenshot of every device in light and dark mode. A violation goes
back to the planner or the coder first; what they cannot fix, the user decides. At the end the
user approves the app, and only then is it ready for Apple.
"""

import asyncio
import base64
import re
from collections.abc import Awaitable, Callable
from dataclasses import replace
from pathlib import Path
from typing import Any

from forge import prompts
from forge.agent import run_agent
from forge.apple_listing import prepare_listing
from forge.apple_review import review_plan, review_product, review_prompt
from forge.ctx import Ctx
from forge.events import GuidelineReview
from forge.plan import Plan, Question, TaskSpec
from forge.ports import AppleBuilder, AppleBuildError, ApplePlatform, AppleScreen
from forge.questions import ask
from forge.release_flow import CheckpointStopped, Outcome, Replan
from forge.runtime.apple import build_report
from forge.tools import keep_screen

GO_ON = "Go on anyway"
STOP = "Stop here"
APPROVE = "Ready for Apple"
SEND_BACK = "Send it back to the agent"
NOT_YET = "Not yet"
FIX_ROUNDS = 2  # the agent's own attempts before the user decides
SEND_BACKS = 3  # rounds of the user's feedback
NO_TESTS = "is not currently configured for the test action"
SCREENS = Path(".forge") / "out" / "apple"  # in the project, so the user can open them
PLATFORMS: dict[str, ApplePlatform] = {"iOS": "ios", "macOS": "macos", "watchOS": "watchos"}


class AppleStopped(CheckpointStopped):
    """The run stops at an Apple checkpoint: the user's choice, or the default without one."""


AppleOutcome = Outcome  # whether the app is ready for Apple, and why (not)


class AppleCheckpoint:
    """The Apple checks as a pipeline checkpoint (S67a): guidelines and the user's approval."""

    name = "apple"

    async def check_request(self, ctx: Ctx, prompt: str) -> None:
        """Review the request against Apple's guidelines."""
        await check_request(ctx, prompt)

    async def check_plan(self, ctx: Ctx, plan: Plan, replan: Replan) -> Plan:
        """Review the plan against Apple's guidelines."""
        return await check_plan(ctx, plan, replan)

    async def finish(self, ctx: Ctx) -> Outcome:
        """Build, check and review the app, then ask the user to approve it."""
        return await finish_product(ctx)

    def report_fields(self, outcome: Outcome) -> dict[str, Any]:
        """`ready_for_apple` and `apple_summary`."""
        return {"ready_for_apple": outcome.ready, "apple_summary": outcome.summary}


async def check_request(ctx: Ctx, prompt: str) -> None:
    """Review the request before anything is planned."""
    await settle(ctx, await review_prompt(ctx, prompt))


async def check_plan(ctx: Ctx, plan: Plan, replan: Callable[[TaskSpec], Awaitable[Plan]]) -> Plan:
    """Review the plan; a violation is planned again with the reviewer's fixes as constraints."""
    for attempt in range(FIX_ROUNDS + 1):
        review = await review_plan(ctx, plan)
        if not blocking(review) or review.error or attempt == FIX_ROUNDS:
            await settle(ctx, review)
            return plan
        spec = plan.spec.model_copy(update={"constraints": [*plan.spec.constraints,
                                                            *fixes(review)]})  # fmt: skip
        plan = await replan(spec)
    return plan


async def finish_product(ctx: Ctx) -> AppleOutcome:
    """Build, check and review the app until it passes, then ask the user to approve it."""
    if ctx.state.apple is None:
        await review_product(ctx, prompts.render("apple_no_builds"))
        return AppleOutcome(False, "The app was reviewed but not built: building it needs a Mac "
                                   "with Xcode, so it cannot be approved yet.")  # fmt: skip
    for _ in range(SEND_BACKS + 1):
        outcome = await checked_product(ctx, ctx.state.apple)
        if outcome is not None:
            return outcome
        feedback = await ask_feedback(ctx)
        if not feedback:
            break
        await work_on(ctx, prompts.render("apple_feedback", material=feedback))
    return AppleOutcome(False, "The app is not approved yet.")


async def checked_product(ctx: Ctx, builder: AppleBuilder) -> AppleOutcome | None:
    """Get the app green and reviewed, then ask for approval (None: the user sent it back)."""
    for attempt in range(FIX_ROUNDS + 1):
        green, builds = await build_everything(ctx, builder)
        review = await review_product(ctx, builds) if green else None
        if review is not None and (not blocking(review) or review.error):
            break
        if attempt == FIX_ROUNDS:
            break
        await work_on(ctx, prompts.render("apple_fix", material=problems(builds, review)))
    if review is None:
        return AppleOutcome(False, f"The app does not build and pass its tests:\n{builds}")
    if blocking(review) and not await user_goes_on(ctx, review):
        return AppleOutcome(False, stop_reason(review))
    choice = await ask_approval(ctx, review)
    if choice == APPROVE:
        listing = await prepare_listing(ctx) if ctx.cfg.apple.listing else ""
        return AppleOutcome(True, f"You approved the app for Apple. {listing}".strip())
    return None if choice == SEND_BACK else AppleOutcome(False, "You have not approved it yet.")


async def build_everything(ctx: Ctx, builder: AppleBuilder) -> tuple[bool, str]:
    """Test (or build, without tests) every platform; when all are green, photograph them."""
    reports, green = [], True
    for platform in apple_platforms(ctx.root):
        try:
            result = await builder.build(platform, "test")
            if not result.ok and NO_TESTS in result.log_tail:
                result = await builder.build(platform, "build")
        except AppleBuildError as err:
            reports.append(f"{platform}: {err} {err.hint}".strip())
            green = False
            continue
        reports.append(build_report(result, ctx.root))
        green = green and result.ok
    if green:
        failed = await take_screens(ctx, builder)
        reports += failed
        green = not failed
    return green, "\n\n".join(reports)


async def take_screens(ctx: Ctx, builder: AppleBuilder) -> list[str]:
    """A screenshot of every device in light and dark mode (the Watch is always dark)."""
    failed = []
    for platform in devices(ctx.root):
        for dark in (True,) if platform == "watchos" else (False, True):
            try:
                screen = await builder.screenshot(platform, dark=dark)
            except AppleBuildError as err:
                failed.append(f"screenshot on {platform}: {err}")
                continue
            ctx.state.apple_shots += 1
            keep_screen(ctx, screen)
            await save_screen(ctx.root, screen)
    return failed


async def save_screen(root: Path, screen: AppleScreen) -> None:
    """Save a screenshot under .forge/out/apple/ for the user to look at."""
    path = root / SCREENS / f"{screen.platform}-{'dark' if screen.dark else 'light'}.png"
    data = base64.b64decode(screen.image.data_b64)

    def write() -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)

    await asyncio.to_thread(write)


def apple_platforms(root: Path) -> list[ApplePlatform]:
    """The platforms the project's targets build for (all three when there is no project.yml)."""
    spec = root / "project.yml"
    if not spec.is_file():
        return ["ios", "macos", "watchos"]
    found = re.findall(r"^\s*platform:\s*(iOS|macOS|watchOS)\b", spec.read_text("utf-8"), re.M)
    return [p for name, p in PLATFORMS.items() if name in found]


def devices(root: Path) -> list[ApplePlatform]:
    """The kinds of device to photograph: iPad too when the iOS app runs on it."""
    platforms = apple_platforms(root)
    spec = root / "project.yml"
    text = spec.read_text("utf-8") if spec.is_file() else ""
    ipad = "ios" in platforms and (
        not text or re.search(r"TARGETED_DEVICE_FAMILY:\s*\"?[\d,]*2", text)
    )
    shown: list[ApplePlatform] = []
    for platform in platforms:
        shown += ["ios", "ipados"] if platform == "ios" and ipad else [platform]
    return shown


async def work_on(ctx: Ctx, task: str) -> None:
    """Let the coder work on problems found after the plan was done."""
    coder = replace(ctx, role="coder")
    await run_agent(coder, task, role="coder", max_turns=ctx.cfg.limits.max_turns_per_step)


def blocking(review: GuidelineReview) -> bool:
    """A violation, or a review that could not be made, needs more than a note."""
    return review.verdict == "violation" or bool(review.error)


def fixes(review: GuidelineReview) -> list[str]:
    """The reviewer's findings that are not ok, as constraints for the next plan."""
    return [f"Apple review ({f.guideline or f.area}): {f.fix or f.reason}"
            for f in review.findings if f.status == "violation"]  # fmt: skip


def problems(builds: str, review: GuidelineReview | None) -> str:
    """What the coder has to fix: the failed builds, or the reviewer's violations."""
    if review is None:
        return builds
    return "\n".join(f"- {f.guideline or f.area}: {f.reason}. Fix: {f.fix or 'see the guideline'}"
                     for f in review.findings if f.status == "violation")  # fmt: skip


def stop_reason(review: GuidelineReview) -> str:
    return f"Stopped at the Apple review of the {review.stage}: {review.summary}"


async def settle(ctx: Ctx, review: GuidelineReview) -> None:
    """A blocking review needs the user's go-ahead; without it the run stops."""
    if blocking(review) and not await user_goes_on(ctx, review):
        raise AppleStopped(stop_reason(review))


async def user_goes_on(ctx: Ctx, review: GuidelineReview) -> bool:
    """Ask whether to go on despite the review (the default is to stop)."""
    question = Question(
        text=f"The Apple review of the {review.stage} found problems: {review.summary}\n"
        + findings_text(review),
        kind="choice", options=[STOP, GO_ON], default=STOP,
        why="Apple would most likely reject the app as it is.",
    )  # fmt: skip
    answers = await ask(ctx, [question])
    if not answers or answers[0] != GO_ON:
        return False
    ctx.state.notes.append(f"You let the {review.stage} go on despite the Apple review: "
                           f"{review.summary}")  # fmt: skip
    return True


async def ask_approval(ctx: Ctx, review: GuidelineReview) -> str:
    """The user's verdict on the finished app (the default is not yet)."""
    question = Question(
        text=f"Is the app ready for Apple? The Apple review: {review.summary}\n"
        + findings_text(review)
        + f"\nScreenshots of every device are in {SCREENS.as_posix()}.",
        kind="choice", options=[NOT_YET, APPROVE, SEND_BACK], default=NOT_YET,
        why="Only an app you approve counts as ready for the App Store.",
    )  # fmt: skip
    answers = await ask(ctx, [question])
    return answers[0] if answers else NOT_YET


async def ask_feedback(ctx: Ctx) -> str:
    """What the user wants changed before they look again."""
    question = Question(text="What should the agent change?", kind="text", default="",
                        why="The agent works on it; then the app is built, checked and shown "
                            "to you again.")  # fmt: skip
    answers = await ask(ctx, [question])
    return answers[0].strip() if answers else ""


def findings_text(review: GuidelineReview) -> str:
    """The findings that are not ok, one per line."""
    lines = [f"- {f.status} ({f.guideline or f.area}): {f.reason}"
             + (f" Fix: {f.fix}" if f.fix else "")
             for f in review.findings if f.status != "ok"]  # fmt: skip
    return "\n".join(lines) or "- no problems found"
