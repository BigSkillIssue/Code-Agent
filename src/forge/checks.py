"""Verify plan steps: run a step's check and record the outcome on the step."""

import sys
import time
from dataclasses import dataclass

from pydantic import BaseModel, Field

from forge import prompts
from forge.ctx import Ctx
from forge.events import PlanUpdated, StepDone
from forge.modelcall import complete
from forge.plan import Step
from forge.ports import Command, SandboxPolicy
from forge.providers.base import ProviderError, text_message
from forge.runtime.files import writable_roots
from forge.runtime.gitops import diff_since
from forge.structured import StructuredError, parse_as

CHECK_TIMEOUT_S = 600
MAX_DIFF_CHARS = 60_000
MAX_NOTE_CHARS = 2_000


@dataclass
class CheckResult:
    """Whether a step's check passed, what it printed, and a short label for the reply."""

    passed: bool
    output: str
    label: str
    timed_out: bool = False


class Verdict(BaseModel):
    """The reviewer's answer to a `review:` check."""

    passed: bool = Field(alias="pass")
    reason: str


def sandbox_policy(ctx: Ctx) -> SandboxPolicy:
    """The session's sandbox settings as the Executor port expects them."""
    roots = [str(p) for p in writable_roots(ctx.root, ctx.cfg)]
    mode = ctx.cfg.sandbox.mode
    return SandboxPolicy(mode=mode, writable_roots=roots, network=ctx.cfg.sandbox.network)


def checkpoint_ref(ctx: Ctx, step: Step) -> str | None:
    """The snapshot taken before the step (set by checkpoints, S18), if any."""
    return ctx.state.checkpoints.get(step.id)


async def verify_step(ctx: Ctx, step: Step) -> CheckResult:
    """Run the step's check: `review:` goes to the reviewer, anything else is a command."""
    if step.check.startswith("review:"):
        return await review_check(ctx, step, step.check.removeprefix("review:").strip())
    return await command_check(ctx, step.check)


async def command_check(ctx: Ctx, check: str) -> CheckResult:
    """Run a check command in the sandbox (bash on POSIX, PowerShell on Windows); exit 0 passes."""
    shell = "powershell" if sys.platform == "win32" else "bash"
    started = time.monotonic()
    cmd = Command(script=check, shell=shell, cwd=str(ctx.root), timeout_s=CHECK_TIMEOUT_S)
    result = await ctx.executor.run(cmd, sandbox_policy(ctx))
    seconds = time.monotonic() - started
    output = "\n".join(t for t in (result.stdout.rstrip(), result.stderr.rstrip()) if t)
    if result.timed_out:
        if result.job_id:
            await ctx.executor.job_stop(result.job_id)
        return CheckResult(False, output, f"{check}, timed out after {CHECK_TIMEOUT_S}s", True)
    label = f"{check}, exit {result.exit_code}, {seconds:.1f}s"
    return CheckResult(result.exit_code == 0, output, label)


async def review_check(ctx: Ctx, step: Step, criterion: str) -> CheckResult:
    """Ask the reviewer role whether the diff since the step began meets the criterion."""
    diff = await diff_since(ctx.root, checkpoint_ref(ctx, step))
    return await review_diff(ctx, criterion, diff)


async def review_diff(ctx: Ctx, criterion: str, diff: str) -> CheckResult:
    """Ask the reviewer role whether a diff meets the criterion."""
    system = prompts.render("reviewer", criterion=criterion, diff=diff[:MAX_DIFF_CHARS])
    try:
        reply, _ = await complete(
            ctx, "reviewer", system, [text_message("user", prompts.render("review_task"))]
        )
        verdict = parse_as(reply.text(), Verdict)
    except (ProviderError, StructuredError) as err:
        return CheckResult(
            False, f"the review could not be completed: {err}", f"review: {criterion}"
        )
    return CheckResult(verdict.passed, verdict.reason, f"review: {criterion}")


async def settle_step(ctx: Ctx, step: Step, summary: str) -> CheckResult:
    """Verify a step and record it: done, another attempt, or failed after the last attempt."""
    result = await verify_step(ctx, step)
    if result.passed:
        step.status, step.notes = "done", summary[:MAX_NOTE_CHARS]
    else:
        step.attempts += 1
        step.notes = f"check failed ({result.label}):\n{result.output[-MAX_NOTE_CHARS:]}"
        if step.attempts >= ctx.cfg.limits.max_step_attempts:
            step.status = "failed"
    if step.status in ("done", "failed"):
        done = StepDone(
            session_id=ctx.session.id,
            agent_id=ctx.agent_id,
            ts=time.time(),
            step_id=step.id,
            ok=result.passed,
        )
        await ctx.bus.publish(done)
    if step.status == "done":
        await ctx.hooks.run(
            "step_done", {"step_id": step.id, "title": step.title, "summary": summary}, ctx
        )
    await save_plan(ctx)
    return result


async def save_plan(ctx: Ctx) -> None:
    """Persist the session and tell renderers the plan changed."""
    await ctx.store.save_session(ctx.session)
    if ctx.session.plan is not None:
        update = PlanUpdated(
            session_id=ctx.session.id, agent_id=ctx.agent_id, ts=time.time(), plan=ctx.session.plan
        )
        await ctx.bus.publish(update)
