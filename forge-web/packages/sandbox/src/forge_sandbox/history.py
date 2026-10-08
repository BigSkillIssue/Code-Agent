"""A chat's memory across turns and worker restarts: its Forge session and earlier turns.

Kept as JSON in FORGE_HOME/chats/<chat id>.json, outside the workspace.
"""

import json
import os
from pathlib import Path

from pydantic import BaseModel, ValidationError

from forge_sandbox import prompts

MAX_TURNS = 10  # earlier turns carried into a follow-up prompt
MAX_PROMPT_CHARS = 2_000
MAX_SUMMARY_CHARS = 3_000
MAX_HISTORY_CHARS = 16_000


class Turn(BaseModel):
    """One finished turn: what the user asked and what came of it."""

    prompt: str
    summary: str
    ok: bool


class ChatState(BaseModel):
    """What a worker needs to pick a chat up again."""

    chat_id: str
    session_id: str | None = None
    turns: list[Turn] = []

    @classmethod
    def load(cls, home: Path, chat_id: str) -> "ChatState":
        """The saved state, or a fresh one."""
        path = state_path(home, chat_id)
        try:
            return cls.model_validate(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, ValueError, ValidationError):
            return cls(chat_id=chat_id)

    def save(self, home: Path) -> None:
        """Write the state atomically."""
        path = state_path(home, self.chat_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_suffix(".tmp")
        temp.write_text(self.model_dump_json(indent=1), encoding="utf-8")
        os.replace(temp, path)


def state_path(home: Path, chat_id: str) -> Path:
    """Where a chat's state lives."""
    return home / "chats" / f"{chat_id}.json"


def shorten(text: str, limit: int) -> str:
    """At most `limit` characters, marking a cut."""
    text = text.strip()
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def with_history(message: str, turns: list[Turn]) -> str:
    """The message itself on the first turn; later, with the conversation so far before it."""
    if not turns:
        return message
    recent = turns[-MAX_TURNS:]
    first_number = len(turns) - len(recent) + 1
    blocks = [
        prompts.turn(
            first_number + i,
            shorten(t.prompt, MAX_PROMPT_CHARS),
            shorten(t.summary or ("(no result)" if t.ok else "(failed)"), MAX_SUMMARY_CHARS),
        )
        for i, t in enumerate(recent)
    ]
    while len(blocks) > 1 and sum(len(b) for b in blocks) > MAX_HISTORY_CHARS:
        blocks.pop(0)
    return prompts.follow_up("\n\n".join(blocks), message)
