"""Slash commands typed by the user (/help lists them); custom commands arrive with S39."""

from typing import Any

from forge.ctx import Ctx
from forge.plan import checklist
from forge.runtime.checkpoint import CheckpointError, restore
from forge.runtime.files import display_path, journal_dir, undo_last_change

HELP = {
    "/plan": "show the plan with step statuses",
    "/compact [hard]": "summarize the context now (hard: reset it)",
    "/context": "tokens used by the last request, by category",
    "/undo": "roll back the last step or file change",
    "/mode [MODE]": "show or set the sandbox mode or approval policy",
    "/jobs": "background jobs and their status",
    "/help": "this list",
}
SANDBOX_MODES = ("read-only", "workspace-write", "full-access")
APPROVAL_POLICIES = ("on-request", "always", "never")


async def run_command(ctx: Ctx, text: str) -> str:
    """Run one slash command and return what to show the user."""
    name, _, args = text.strip().partition(" ")
    if name == "/undo":
        return await undo(ctx)
    if name == "/compact":
        return request_compaction(ctx, hard=args.strip() == "hard")
    if name == "/context":
        return context_report(ctx)
    if name == "/plan":
        return checklist(ctx.session.plan) if ctx.session.plan else "no plan yet"
    if name == "/mode":
        return set_mode(ctx, args.strip())
    if name == "/jobs":
        return jobs_report(ctx)
    if name == "/help":
        return "\n".join(f"{command:<16}{text}" for command, text in HELP.items())
    return f"unknown command {name}"


async def undo(ctx: Ctx) -> str:
    """Roll back the most recent step (its snapshot), or else the last file change."""
    plan = ctx.session.plan
    checkpoints = ctx.state.checkpoints
    if plan is not None and checkpoints:
        order = [s.id for s in plan.steps]
        step_id = max(checkpoints, key=lambda sid: order.index(sid) if sid in order else -1)
        try:
            changed = await restore(ctx.root, checkpoints.pop(step_id))
        except CheckpointError as err:
            return f"undo failed: {err}"
        step = plan.step(step_id)
        if step is not None:
            step.status, step.attempts, step.notes = "todo", 0, ""
            await ctx.store.save_session(ctx.session)
        return f"undid step {step_id}: {len(changed)} files restored"
    restored = undo_last_change(journal_dir(ctx))
    if not restored:
        return "nothing to undo"
    return "undid the last change: " + ", ".join(display_path(ctx.root, p) for p in restored)


def request_compaction(ctx: Ctx, hard: bool) -> str:
    """Compact on the agent's next turn: level 2, or level 3 with `/compact hard`."""
    ctx.state.compact_request = 3 if hard else 2
    return f"the context will be {'reset' if hard else 'summarized'} before the next model call"


def context_report(ctx: Ctx) -> str:
    """Tokens of the latest request by category, against the budget."""
    usage = dict(ctx.state.context_usage)
    if not usage:
        return "no model request yet"
    total, budget = usage.pop("total"), usage.pop("budget")
    share = f"{total / budget:.0%}" if budget else "?"
    lines = [f"context: {total:,} of {budget:,} tokens ({share})"]
    lines += [f"  {name:<13}{tokens:>9,}" for name, tokens in usage.items()]
    limits = ctx.cfg.limits
    lines.append(f"summary at {limits.compact_at:.0%}, reset at {limits.reset_at:.0%}")
    return "\n".join(lines)


def set_mode(ctx: Ctx, mode: str) -> str:
    """Show or change the sandbox mode or the approval policy for this session."""
    cfg = ctx.cfg
    if mode in SANDBOX_MODES:
        cfg.sandbox.mode = mode  # type: ignore[assignment]
    elif mode in APPROVAL_POLICIES:
        cfg.approval.policy = mode  # type: ignore[assignment]
    elif mode:
        return f"unknown mode {mode}; use one of {', '.join(SANDBOX_MODES + APPROVAL_POLICIES)}"
    return f"sandbox: {cfg.sandbox.mode}, approval: {cfg.approval.policy}"


def jobs_report(ctx: Ctx) -> str:
    """One line per background job of this session's executor."""
    jobs: dict[str, Any] = getattr(ctx.executor, "jobs", {})
    if not jobs:
        return "no background jobs"
    lines = []
    for job in jobs.values():
        state = "running" if job.ended is None else f"exit {job.exit_code}"
        lines.append(f"{job.id}  {state:<9} {job.elapsed():.0f}s  pid {job.proc.pid}")
    return "\n".join(lines)
