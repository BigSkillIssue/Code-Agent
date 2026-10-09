"""The pipeline every task goes through: refine -> clarify -> plan -> execute -> verify."""

import json
from dataclasses import replace
from typing import Literal

from pydantic import BaseModel

from forge import prompts
from forge.agent import over_budget, run_agent
from forge.apple_flow import AppleStopped, check_plan, check_request, finish_product
from forge.checks import CheckResult, save_plan, settle_step
from forge.checks import verify_step as run_check
from forge.context import gather
from forge.ctx import Ctx
from forge.modelcall import complete, publish_error
from forge.plan import Plan, Question, Step, TaskSpec, checklist
from forge.providers.base import Message, ProviderError, Usage, text_message
from forge.questions import ask, assumption, default_answer
from forge.runtime.checkpoint import CheckpointError, snapshot
from forge.runtime.gitops import changed_files, diff_since
from forge.structured import StructuredError, parse_as


class Report(BaseModel):
    """What the user reads at the end of a task."""

    ok: bool
    summary: str
    files_changed: list[str]
    assumptions: list[str]
    manual_checks: list[str]
    usage: Usage
    ready_for_apple: bool = False  # S60: only when the user approved the app
    apple_summary: str = ""  # S60: why the app is (not) ready for Apple


class ReviewAnswer(BaseModel):
    """The reviewer's final verdict."""

    ok: bool
    summary: str
    manual_checks: list[str] = []


class PipelineError(Exception):
    """A pipeline stage could not produce its result (e.g. no valid spec after a retry)."""


class PlanRejected(PipelineError):
    """The user rejected the plan without asking for changes."""


async def refine(prompt: str, ctx: Ctx) -> TaskSpec:
    """Turn the raw prompt into a TaskSpec with the refiner role (one retry on invalid JSON)."""
    schema = TaskSpec.model_json_schema()
    system = prompts.render("refiner", schema=json.dumps(schema), context=await gather(ctx))
    messages = [text_message("user", prompt)]
    return await ask_for_spec(ctx, system, messages, schema)


async def ask_for_spec(
    ctx: Ctx, system: str, messages: list[Message], schema: dict[str, object]
) -> TaskSpec:
    """Ask the refiner for a TaskSpec; on an unusable answer, say why and ask once more."""
    problem = ""
    for _ in range(2):
        reply, _usage = await complete(ctx, "refiner", system, messages, json_schema=schema)
        try:
            spec = parse_as(reply.text(), TaskSpec)
            if not spec.acceptance_criteria:
                raise StructuredError("acceptance_criteria needs at least one entry")
            return spec
        except StructuredError as err:
            problem = str(err)
            messages = [
                *messages,
                reply,
                text_message("user", prompts.render("fix_json", failure=problem)),
            ]
    raise PipelineError(f"the refiner gave no valid task specification: {problem}")


GO = "/go"


async def clarify(spec: TaskSpec, ctx: Ctx) -> TaskSpec:
    """Ask the spec's open questions, merge the answers, repeat (max rounds, or until /go)."""
    for _ in range(ctx.cfg.limits.max_clarify_rounds):
        questions = spec.open_questions[:4]
        if not questions:
            break
        answers = await ask(ctx, questions)
        if answers is None or ctx.headless:
            return assume_rest(spec, questions, answers)
        if GO in answers:
            return assume_rest(
                spec,
                questions,
                [
                    a if a != GO else default_answer(q)
                    for q, a in zip(questions, answers, strict=True)
                ],
            )
        spec = await merge_answers(ctx, spec, questions, answers)
    if spec.open_questions:
        spec = assume_rest(spec, spec.open_questions, None)
    return spec


def assume_rest(spec: TaskSpec, questions: list[Question], answers: list[str] | None) -> TaskSpec:
    """Close the questions with these answers (or defaults), recorded as assumptions."""
    picked = answers or [default_answer(q) for q in questions]
    notes = [assumption(q, a) for q, a in zip(questions, picked, strict=True)]
    known = set(spec.assumptions)
    added = [n for n in notes if n not in known]
    return spec.model_copy(
        update={"assumptions": [*spec.assumptions, *added], "open_questions": []}
    )


async def merge_answers(
    ctx: Ctx, spec: TaskSpec, questions: list[Question], answers: list[str]
) -> TaskSpec:
    """Let the refiner fold the answers into the spec and look for remaining gaps."""
    schema = TaskSpec.model_json_schema()
    system = prompts.render("refiner", schema=json.dumps(schema), context=await gather(ctx))
    lines = "\n".join(f"- {q.text} -> {a}" for q, a in zip(questions, answers, strict=True))
    note = prompts.render("merge_answers", spec=spec.model_dump_json(indent=2), context=lines)
    return await ask_for_spec(ctx, system, [text_message("user", note)], schema)


PLANNER_TURNS = 30


async def make_plan(spec: TaskSpec, ctx: Ctx) -> Plan:
    """Let the planner study the code and submit a plan the user approves."""
    ctx.session.spec = spec
    ctx.session.plan = None
    ctx.state.plan_rejected = False
    planner = replace(ctx, role="planner")
    await run_agent(planner, prompts.render("plan_task"), role="planner", max_turns=PLANNER_TURNS)
    if ctx.state.plan_rejected:
        raise PlanRejected("the user rejected the plan")
    if ctx.session.plan is None:
        raise PipelineError("the planner did not submit a valid plan")
    return ctx.session.plan


MAX_REPLANS = 3


Mode = Literal["solo", "subagents", "team"]
MODE_BY_SIZE: dict[str, Mode] = {
    "trivial": "solo",
    "small": "solo",
    "medium": "subagents",
    "large": "team",
}


def choose_mode(spec: TaskSpec, override: str | None) -> Mode:
    """--solo / --team win; otherwise the task's size decides."""
    if override in ("solo", "subagents", "team"):
        return override  # type: ignore[return-value]
    return MODE_BY_SIZE[spec.size]


async def execute(plan: Plan, ctx: Ctx) -> Plan:
    """Run the plan one ready step at a time; a step that keeps failing triggers a replan.

    In team mode, background workers first take tasks from the board; what they leave
    is then done here one step at a time. Execution stops once the budget is spent.
    """
    ctx.session.plan = plan
    if ctx.state.mode == "team" and ctx.state.team is not None and not over_budget(ctx):
        await ctx.state.team.run_team(ctx, plan)
    replans = 0
    while not over_budget(ctx) and (step := ctx.session.plan.next_ready_step()) is not None:
        await run_step(ctx, step)
        if step.status == "failed" and replans < MAX_REPLANS:
            replans += 1
            await replan(ctx.session.plan, step, ctx)
    return ctx.session.plan


async def run_step(ctx: Ctx, step: Step) -> None:
    """Work on one step until its check passes or its attempts run out."""
    step.status = "doing"
    await save_plan(ctx)
    await before_step(ctx, step)
    worker = replace(ctx, role=step.role)
    while step.status == "doing":
        assert ctx.session.plan is not None
        task = prompts.render("step", step=step_brief(step), plan=checklist(ctx.session.plan))
        result = await run_agent(
            worker, task, role=step.role, max_turns=ctx.cfg.limits.max_turns_per_step
        )
        if step.status == "doing":
            # The agent stopped without a passing finish_step: check the work ourselves.
            await settle_step(ctx, step, result.text)


async def before_step(ctx: Ctx, step: Step) -> None:
    """Snapshot the working tree so /undo can roll this step back."""
    try:
        ref = await snapshot(ctx.root, ctx.session.id, step.id)
    except CheckpointError as err:
        await publish_error(ctx, f"no checkpoint before {step.id}: {err}")
        return
    if ref is not None:
        ctx.state.checkpoints[step.id] = ref


def step_brief(step: Step) -> str:
    """The step as the STEP prompt shows it, including the last failure if there was one."""
    lines = [f"Step {step.id}: {step.title}", step.detail]
    if step.files:
        lines.append("Files: " + ", ".join(step.files))
    lines.append(f"Check: {step.check}")
    if step.attempts:
        lines.append(f"Previous attempt {step.attempts}: {step.notes}")
    return "\n".join(lines)


async def verify_step(ctx: Ctx, step: Step) -> CheckResult:
    """Run a step's check (shell command, or `review:` criterion judged by the reviewer)."""
    return await run_check(ctx, step)


async def replan(plan: Plan, step: Step, ctx: Ctx) -> Plan:
    """Let the replanner replace the steps that are not done, given why `step` failed."""
    ctx.state.failure = (
        f"Step {step.id} ({step.title}) failed after {step.attempts} attempts.\n{step.notes}"
    )
    replanner = replace(ctx, role="replanner")
    await run_agent(
        replanner, prompts.render("replan_task"), role="replanner", max_turns=PLANNER_TURNS
    )
    ctx.state.failure = ""
    assert ctx.session.plan is not None
    return ctx.session.plan


async def resume(ctx: Ctx) -> Plan:
    """Continue a saved session's plan at its first unfinished step."""
    plan = ctx.session.plan
    if plan is None:
        raise PipelineError("this session has no plan to resume")
    for step in plan.steps:
        if step.status == "doing":  # it was interrupted: start it over
            step.status = "todo"
    ctx.session.status = "active"
    return await execute(plan, ctx)


MAX_REVIEW_DIFF = 80_000


async def final_review(plan: Plan, ctx: Ctx) -> Report:
    """The reviewer checks the whole diff against the acceptance criteria and writes the report."""
    base = first_checkpoint(ctx, plan)
    diff = (await diff_since(ctx.root, base))[:MAX_REVIEW_DIFF]
    system = prompts.render("final_review", spec=plan.spec.model_dump_json(indent=2), diff=diff)
    all_done = all(s.status in ("done", "skipped") for s in plan.steps)
    try:
        reply, _ = await complete(
            ctx, "reviewer", system, [text_message("user", prompts.render("review_task"))]
        )
        answer = parse_as(reply.text(), ReviewAnswer)
    except (ProviderError, StructuredError) as err:
        answer = ReviewAnswer(
            ok=all_done,
            summary=f"The final review could not run ({err}).",
            manual_checks=["review the diff by hand"],
        )
    return Report(
        ok=answer.ok and all_done,
        summary=answer.summary,
        files_changed=await changed_files(ctx.root, base),
        assumptions=assumptions_of(ctx, plan.spec),
        manual_checks=answer.manual_checks,
        usage=ctx.state.usage,
    )


def first_checkpoint(ctx: Ctx, plan: Plan) -> str | None:
    """The snapshot taken before the first step that has one (the task's starting point)."""
    return next(
        (ctx.state.checkpoints[s.id] for s in plan.steps if s.id in ctx.state.checkpoints), None
    )


def assumptions_of(ctx: Ctx, spec: TaskSpec | None) -> list[str]:
    """Every assumption made on the user's behalf, without repeats."""
    found = [*(spec.assumptions if spec else []), *ctx.state.notes]
    return list(dict.fromkeys(found))


async def run_task(prompt: str, ctx: Ctx) -> Report:
    """The whole pipeline: refine, clarify, plan, execute, final review."""
    ctx.session.status = "active"
    submitted = await ctx.hooks.run("prompt_submit", {"prompt": prompt}, ctx)
    if submitted.block:
        return stopped_report(ctx, f"A prompt_submit hook stopped this task: {submitted.message}")
    apple = ctx.cfg.apple.review
    try:
        if apple:
            await check_request(ctx, prompt)
        spec = await refine(prompt, ctx)
        ctx.session.spec = spec
        ctx.state.mode = choose_mode(spec, ctx.state.mode_override)
        if spec.size == "trivial" and not apple:  # an Apple app's plan is always reviewed
            report = await run_trivial(prompt, spec, ctx)
        else:
            report = await run_planned(await clarify(spec, ctx), ctx)
    except PlanRejected:
        report = stopped_report(ctx, "You rejected the plan, so nothing was changed.")
    except (PipelineError, AppleStopped) as err:
        report = stopped_report(ctx, str(err))
    ctx.session.status = "done" if report.ok else "failed"
    ctx.session.summary = report.summary
    await ctx.store.save_session(ctx.session)
    await ctx.hooks.run("stop", {"ok": report.ok, "summary": report.summary}, ctx)
    return report


async def run_planned(spec: TaskSpec, ctx: Ctx) -> Report:
    """Plan, execute and review; with Apple checks, the plan and the built app are reviewed too."""
    plan = await make_plan(spec, ctx)
    if ctx.cfg.apple.review:
        plan = await check_plan(ctx, plan, lambda revised: make_plan(revised, ctx))
    plan = await execute(plan, ctx)
    if over_budget(ctx):
        return await budget_report(ctx)
    if not ctx.cfg.apple.review:
        return await final_review(plan, ctx)
    outcome = await finish_product(ctx)  # before the final review: fixes belong in its diff
    report = await final_review(plan, ctx)
    return report.model_copy(
        update={"ready_for_apple": outcome.ready, "apple_summary": outcome.summary}
    )


async def run_trivial(prompt: str, spec: TaskSpec, ctx: Ctx) -> Report:
    """Trivial tasks skip questions and planning: one agent run straight on the prompt."""
    result = await run_agent(ctx, prompt, max_turns=ctx.cfg.limits.max_turns_per_step)
    return Report(
        ok=result.stopped == "done",
        summary=result.text,
        files_changed=await changed_files(ctx.root),
        assumptions=assumptions_of(ctx, spec),
        manual_checks=[],
        usage=ctx.state.usage,
    )


async def budget_report(ctx: Ctx) -> Report:
    """The report when the session's cost budget ran out before the plan was finished."""
    plan = ctx.session.plan
    left = [s for s in plan.steps if s.status not in ("done", "skipped")] if plan else []
    limit = ctx.cfg.limits.max_cost_usd
    summary = f"Stopped: the cost budget of ${limit:.2f} was used up."
    if left:
        summary += " Not finished: " + ", ".join(f"{s.id} {s.title}" for s in left) + "."
    return Report(
        ok=False,
        summary=summary + " Run `forge resume` with a higher limits.max_cost_usd to continue.",
        files_changed=await changed_files(ctx.root),
        assumptions=assumptions_of(ctx, ctx.session.spec),
        manual_checks=[],
        usage=ctx.state.usage,
    )


def stopped_report(ctx: Ctx, why: str) -> Report:
    """A report for a task that ended before any work was done."""
    return Report(
        ok=False,
        summary=why,
        files_changed=[],
        assumptions=assumptions_of(ctx, ctx.session.spec),
        manual_checks=[],
        usage=ctx.state.usage,
    )


def report_text(report: Report) -> str:
    """The report as plain text for the terminal and SessionDone."""
    lines = [report.summary]
    if report.files_changed:
        lines.append("Files changed: " + ", ".join(report.files_changed))
    lines += [f"Assumed: {a}" for a in report.assumptions]
    lines += [f"Check by hand: {c}" for c in report.manual_checks]
    if report.apple_summary:
        ready = "yes" if report.ready_for_apple else "no"
        lines.append(f"Ready for Apple: {ready} ({report.apple_summary})")
    usage = report.usage
    tokens = f"{usage.input_tokens} in, {usage.output_tokens} out tokens"
    lines.append(f"Cost: ${usage.cost_usd:.4f} ({tokens})")
    return "\n".join(lines)
