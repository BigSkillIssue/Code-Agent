"""Slash commands typed by the user (/undo, /compact, /context); more arrive with S39."""

from forge.ctx import Ctx
from forge.runtime.checkpoint import CheckpointError, restore
from forge.runtime.files import display_path, journal_dir, undo_last_change


async def run_command(ctx: Ctx, text: str) -> str:
    """Run one slash command and return what to show the user."""
    name, _, args = text.strip().partition(" ")
    if name == "/undo":
        return await undo(ctx)
    if name == "/compact":
        return request_compaction(ctx, hard=args.strip() == "hard")
    if name == "/context":
        return context_report(ctx)
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
