"""Ask the user questions, or answer them with defaults when nobody is there (headless)."""

import time

from forge.ctx import Ctx
from forge.events import QuestionAsked
from forge.plan import Question


def default_answer(question: Question) -> str:
    """The answer used without a user: the default, else the first option, "no", or ""."""
    if question.default is not None:
        return question.default
    if question.kind == "choice" and question.options:
        return question.options[0]
    return "no" if question.kind == "confirm" else ""


def assumption(question: Question, answer: str) -> str:
    """How an answer given on the user's behalf is recorded."""
    return f'Assumed for "{question.text}": {answer}'


async def ask(ctx: Ctx, questions: list[Question]) -> list[str] | None:
    """One answer per question (multi joined by ", "); None when the user dismissed them.

    Headless sessions answer with defaults and record each one as an assumption.
    """
    await ctx.bus.publish(
        QuestionAsked(
            session_id=ctx.session.id, agent_id=ctx.agent_id, ts=time.time(), questions=questions
        )
    )
    if ctx.headless:
        answers = [default_answer(q) for q in questions]
        notes = [assumption(q, a) for q, a in zip(questions, answers, strict=True)]
        if ctx.session.spec is not None:
            ctx.session.spec.assumptions.extend(notes)
        else:
            ctx.state.notes.extend(notes)
        return answers
    replies = await ctx.renderer.ask(questions)
    if not replies:
        return None
    by_index = {r.question_index: ", ".join(r.values) for r in replies}
    return [by_index.get(i, default_answer(q)) for i, q in enumerate(questions)]
