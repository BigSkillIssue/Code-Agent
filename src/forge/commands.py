"""Slash commands typed by the user (/undo, ...); more arrive with S39."""

from forge.ctx import Ctx
from forge.runtime.checkpoint import CheckpointError, restore
from forge.runtime.files import display_path, journal_dir, undo_last_change


async def run_command(ctx: Ctx, text: str) -> str:
    """Run one slash command and return what to show the user."""
    name, _, _args = text.strip().partition(" ")
    if name == "/undo":
        return await undo(ctx)
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
