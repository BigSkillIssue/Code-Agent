"""The pipeline every task goes through: refine -> clarify -> plan -> execute -> verify."""

import json

from forge import prompts
from forge.agent import complete
from forge.context import gather
from forge.ctx import Ctx
from forge.plan import TaskSpec
from forge.providers.base import Message, text_message
from forge.structured import StructuredError, parse_as


class PipelineError(Exception):
    """A pipeline stage could not produce its result (e.g. no valid spec after a retry)."""


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
