"""The fake model for development and tests: no API keys, scripted answers."""

import json
from pathlib import Path
from typing import Any

HELLO_SPEC = {
    "goal": "greet the user", "context": "", "requirements": [], "constraints": [],
    "acceptance_criteria": ["the user is greeted"], "assumptions": [], "open_questions": [],
    "size": "trivial",
}  # fmt: skip
GREETING = "Hello from Forge Web's fake model. Nothing to do."
BUILTIN_FAKE: dict[str, Any] = {
    "roles": {"refiner": [{"text": json.dumps(HELLO_SPEC)}] * 200},
    "turns": [{"text": GREETING}] * 200,
}


def fake_script(path: str = "") -> dict[str, Any]:
    """A script file's contents, or the built-in greeting."""
    if not path:
        return BUILTIN_FAKE
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    return data if isinstance(data, dict) else {"turns": data}
