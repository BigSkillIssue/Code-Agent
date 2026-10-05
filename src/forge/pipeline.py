"""The pipeline every task goes through: refine -> clarify -> plan -> execute -> verify."""

import json
from dataclasses import replace

from forge import prompts
from forge.agent import complete, run_agent
from forge.context import gather
from forge.ctx import Ctx
from forge.plan import Plan, Question, TaskSpec
from forge.providers.base import Message, text_message
from forge.questions import ask, assumption, default_answer
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
