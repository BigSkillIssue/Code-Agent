"""A chat's memory across worker restarts: which Forge session it is, and its turns so far.

Kept as JSON in FORGE_HOME/chats/<chat id>.json, outside the workspace. What the model sees of
earlier turns is Forge's own conversation in that session (S76, W46): it folds old turns into
notes and the agent tidies it with compact_context, so the worker no longer adds a history.
"""

import json
import os
from pathlib import Path

from pydantic import BaseModel, ValidationError


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
