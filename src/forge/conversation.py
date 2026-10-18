"""One ongoing conversation per session (S76): every finished task becomes a turn the next
task sees, and the oldest turns are folded into short notes by the compressor so the
conversation never outgrows its share of the context. `compact_context` lets the agent drop
it all itself when a new task starts, keeping only what it says still matters."""

import time

from forge import prompts
from forge.ctx import Ctx
from forge.modelcall import complete
from forge.ports import NOTES_CHARS, Turn
from forge.providers.base import ProviderError, text_message

MAX_TURN_CHARS = 12_000  # the recent turns kept word for word
KEEP_TURNS = 2  # the newest turns are never folded
PROMPT_CHARS = 4_000  # of each turn's request
RESULT_CHARS = 2_000  # of each turn's result
MAX_FILES = 30  # files listed per turn


def conversation_text(ctx: Ctx) -> str:
    """The conversation so far for the lead agent's prompts (empty when there is none)."""
    talk = ctx.session.conversation
    if ctx.agent_id != "main" or not (talk.notes or talk.turns):
        return ""
    turns = "\n".join(turn_text(turn) for turn in talk.turns)
    return prompts.render("conversation", notes=talk.notes or "(none)", turns=turns or "(none)")


def turn_text(turn: Turn) -> str:
    """One turn as the prompts show it."""
    status = "done" if turn.ok else "not finished"
    files = f"\nFiles changed: {', '.join(turn.files)}" if turn.files else ""
    return f"<turn>\nUser: {turn.prompt}\nResult ({status}): {turn.result}{files}\n</turn>"


async def record_turn(ctx: Ctx, prompt: str, ok: bool, summary: str, files: list[str]) -> None:
    """Add a finished task to the conversation; fold the oldest turns when it grows too long."""
    turn = Turn(
        prompt=prompt[:PROMPT_CHARS],
        result=summary[:RESULT_CHARS],
        files=files[:MAX_FILES],
        ok=ok,
        ts=time.time(),
    )
    ctx.session.conversation.turns.append(turn)
    await fold(ctx)


def fold_point(turns: list[Turn]) -> int:
    """How many of the oldest turns to fold: none while they fit, else enough to leave half
    the room free (the newest KEEP_TURNS always stay)."""
    sizes = [len(turn_text(turn)) for turn in turns]
    rest = sum(sizes)
    if rest <= MAX_TURN_CHARS:
        return 0
    split = 0
    while split < len(turns) - KEEP_TURNS and rest > MAX_TURN_CHARS // 2:
        rest -= sizes[split]
        split += 1
    return split


async def fold(ctx: Ctx) -> None:
    """Fold the turns that no longer fit into the notes, with the compressor role."""
    talk = ctx.session.conversation
    split = fold_point(talk.turns)
    if split == 0:
        return
    old, talk.turns = talk.turns[:split], talk.turns[split:]
    text = "\n".join(turn_text(turn) for turn in old)
    system = prompts.render("fold_turns", notes=talk.notes or "(none)", turns=text)
    try:
        reply, _ = await complete(
            ctx, "compressor", system, [text_message("user", prompts.render("compress_task"))]
        )
        notes = reply.text().strip()
    except ProviderError:
        notes = ""
    # Without a note from the compressor the old turns stay, as plain text, in the notes.
    talk.notes = (notes or f"{talk.notes}\n{text}".strip())[-NOTES_CHARS:]
