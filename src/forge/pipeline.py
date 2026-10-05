"""The pipeline every task goes through: refine -> clarify -> plan -> execute -> verify."""

import json
from dataclasses import replace

from forge import prompts
from forge.agent import run_agent
from forge.checks import CheckResult, save_plan, settle_step
from forge.checks import verify_step as run_check
from forge.context import gather
from forge.ctx import Ctx
from forge.modelcall import complete, publish_error
from forge.plan import Plan, Question, Step, TaskSpec, checklist
from forge.providers.base import Message, text_message
from forge.questions import ask, assumption, default_answer
from forge.runtime.checkpoint import CheckpointError, snapshot
from forge.structured import StructuredError, parse_as


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


async def execute(plan: Plan, ctx: Ctx) -> Plan:
    """Run the plan one ready step at a time; a step that keeps failing triggers a replan."""
    ctx.session.plan = plan
    replans = 0
    while (step := ctx.session.plan.next_ready_step()) is not None:
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
