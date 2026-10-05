"""The team board: plan steps seen as tasks with an owner (docs/TOOLS.md: Team board).

Status comes from the step; a `todo` step is `ready` when all its dependencies are done or
skipped, else `blocked`. Owners live in the store (`BoardStore`), so a claim is atomic even
when several agents race for the same task.
"""

from collections.abc import Callable

from forge.checks import save_plan, settle_step
from forge.ctx import Ctx
from forge.plan import Plan, Step
from forge.ports import BoardStore
from forge.runtime.errors import ToolError
from forge.tools import step_failed

FINISHED = ("done", "skipped")
Notify = Callable[[str], None]


def display_status(plan: Plan, step: Step) -> str:
    """ready / blocked for todo steps, otherwise the step's own status."""
    if step.status != "todo":
        return step.status
    deps = [plan.step(d) for d in step.depends_on]
    return "ready" if all(d is not None and d.status in FINISHED for d in deps) else "blocked"


def board_store(ctx: Ctx) -> BoardStore:
    """The store as a board; unsupported for stores without one."""
    if not isinstance(ctx.store, BoardStore):
        raise ToolError("unsupported", "this session's store has no task board")
    return ctx.store


def plan_of(ctx: Ctx) -> Plan:
    """The session's plan, or invalid_args before there is one."""
    if ctx.session.plan is None:
        raise ToolError("invalid_args", "there is no plan yet")
    return ctx.session.plan


async def read_board(ctx: Ctx, statuses: list[str] | None) -> str:
    """read_board: one row per task with status, owner and dependencies."""
    plan = plan_of(ctx)
    owners = await board_store(ctx).owners(ctx.session.id)
    rows = [f"{'id':<4}{'status':<9}{'owner':<7}{'after':<8}title"]
    for step in plan.steps:
        status = display_status(plan, step)
        if statuses and status not in statuses:
            continue
        after = ",".join(step.depends_on) or "-"
        rows.append(f"{step.id:<4}{status:<9}{owners.get(step.id, '-'):<7}{after:<8}{step.title}")
    return "\n".join(rows)


async def claim(ctx: Ctx, task_id: str) -> str:
    """claim_task: take a ready task (one per agent at a time)."""
    plan = plan_of(ctx)
    step = plan.step(task_id)
    if step is None:
        raise ToolError("not_found", f"there is no task {task_id}")
    board = board_store(ctx)
    owners = await board.owners(ctx.session.id)
    mine = [s for s, owner in owners.items() if owner == ctx.agent_id and doing(plan, s)]
    if mine:
        raise ToolError("invalid_args", "you already own a task", hint=f"finish {mine[0]} first")
    status = display_status(plan, step)
    if status == "doing":
        raise ToolError("busy", f"{task_id} is taken", hint=f"owned by {owners.get(task_id, '?')}")
    if status != "ready":
        waiting = [
            d for d in step.depends_on if (dep := plan.step(d)) and dep.status not in FINISHED
        ]
        hint = f"waiting for {', '.join(waiting)}" if status == "blocked" else f"already {status}"
        raise ToolError("invalid_args", f"{task_id} is not ready", hint=hint)
    owner = await board.claim(ctx.session.id, task_id, ctx.agent_id)
    if owner != ctx.agent_id:
        raise ToolError("busy", f"{task_id} is taken", hint=f"owned by {owner}")
    step.status = "doing"
    await save_plan(ctx)
    lines = [f"claimed {step.id}: {step.title}", f"detail: {step.detail or '-'}"]
    lines += [f"files: {', '.join(step.files) or '-'}", f"check: {step.check}"]
    return "\n".join(lines)


def doing(plan: Plan, step_id: str) -> bool:
    """True if the step is being worked on."""
    step = plan.step(step_id)
    return step is not None and step.status == "doing"


async def update(ctx: Ctx, task_id: str, status: str, result: str, notify: Notify) -> str:
    """update_task: progress, done (verified like finish_step) or failed."""
    plan = plan_of(ctx)
    step = plan.step(task_id)
    if step is None:
        raise ToolError("not_found", f"there is no task {task_id}")
    owner = (await board_store(ctx).owners(ctx.session.id)).get(task_id)
    if owner != ctx.agent_id:
        raise ToolError(
            "permission_denied", f"you do not own {task_id}", hint=f"owned by {owner or 'nobody'}"
        )
    if len(result) > 4000:
        raise ToolError("invalid_args", "result must be at most 4000 characters")
    if status == "doing":
        step.notes = result
        await save_plan(ctx)
        notify(f"[task {task_id} progress from {ctx.agent_id}] {result}")
        return f"{task_id}: progress noted; lead notified"
    if status == "failed":
        return await fail(ctx, step, result, notify)
    return await finish(ctx, step, result, notify)


async def finish(ctx: Ctx, step: Step, result: str, notify: Notify) -> str:
    """Run the step's check; done only when it passes."""
    if step.status != "doing":
        raise ToolError("invalid_args", f"{step.id} is {step.status}, not doing")
    check = await settle_step(ctx, step, result)
    if check.passed:
        notify(f"[task {step.id} done by {ctx.agent_id}] {result}")
        return f"{step.id} done: check passed; lead notified"
    body = "--- check output ---\n" + (check.output or "(no output)")
    if step_failed(step):  # settle_step changed the status after the last attempt
        await board_store(ctx).release(ctx.session.id, step.id)
        notify(f"[task {step.id} failed by {ctx.agent_id}] check failed {step.attempts} times")
    raise ToolError("check_failed", f"{step.id} check failed ({check.label})", body=body)


async def fail(ctx: Ctx, step: Step, result: str, notify: Notify) -> str:
    """Mark the task failed, release it and tell the lead."""
    if not result.strip():
        raise ToolError("invalid_args", "say why the task failed in result")
    step.status, step.notes = "failed", result
    await board_store(ctx).release(ctx.session.id, step.id)
    await save_plan(ctx)
    notify(f"[task {step.id} failed by {ctx.agent_id}] {result}")
    return f"{step.id} marked failed; lead notified"


async def release_owned(ctx: Ctx, agent_id: str) -> list[str]:
    """Give an agent's unfinished tasks back to the board (status todo)."""
    plan = ctx.session.plan
    if plan is None or not isinstance(ctx.store, BoardStore):
        return []
    released = []
    for step_id, owner in (await ctx.store.owners(ctx.session.id)).items():
        step = plan.step(step_id)
        if owner == agent_id and step is not None and step.status == "doing":
            step.status = "todo"
            await ctx.store.release(ctx.session.id, step_id)
            released.append(step_id)
    if released:
        await save_plan(ctx)
    return released
