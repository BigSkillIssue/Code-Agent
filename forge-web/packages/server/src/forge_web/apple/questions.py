"""Forge's final question in an Apple chat: "Is the app ready for Apple?" (`forge.apple_flow`).

Forge asks it as one choice with fixed options; the web UI shows it as the approval, and only
the answer "Ready for Apple" to it is recorded as the user's approval.
"""

from typing import Any

from forge.apple_flow import APPROVE, NOT_YET, SEND_BACK

OPTIONS = [NOT_YET, APPROVE, SEND_BACK]
PURPOSE = "apple_approval"  # how a request item says that it is the approval


def is_approval(payload: Any) -> bool:
    """Whether a question request's payload is Forge's approval question."""
    questions = payload.get("questions") if isinstance(payload, dict) else None
    if not isinstance(questions, list) or len(questions) != 1:
        return False
    question = questions[0]
    return (
        isinstance(question, dict)
        and question.get("kind") == "choice"
        and question.get("options") == OPTIONS
    )


def chosen(answer: Any) -> str:
    """The one option an answer chose ("" when it chose none, or several)."""
    answers = answer.get("answers") if isinstance(answer, dict) else None
    if not isinstance(answers, list) or len(answers) != 1 or not isinstance(answers[0], dict):
        return ""
    values = answers[0].get("values")
    if not isinstance(values, list) or len(values) != 1 or not isinstance(values[0], str):
        return ""
    return values[0]
