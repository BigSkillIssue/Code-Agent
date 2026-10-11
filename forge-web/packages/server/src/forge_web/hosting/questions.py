"""Forge's final question in an app chat: "Ready to go live?" (`forge.app_flow`).

Forge asks it as one choice with fixed options; only the answer "Ready to go live" to it is
recorded as the user's approval of the commit (`golive.py`).
"""

from typing import Any

from forge.app_flow import GO_LIVE, NOT_YET, SEND_BACK

OPTIONS = [NOT_YET, GO_LIVE, SEND_BACK]
PURPOSE = "app_go_live"  # how a request item says that it is the go-live question


def is_go_live(payload: Any) -> bool:
    """Whether a question request's payload is Forge's go-live question."""
    questions = payload.get("questions") if isinstance(payload, dict) else None
    if not isinstance(questions, list) or len(questions) != 1:
        return False
    question = questions[0]
    return (
        isinstance(question, dict)
        and question.get("kind") == "choice"
        and question.get("options") == OPTIONS
    )
