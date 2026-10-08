"""Model-facing text that Forge Web adds on top of Forge's own prompts (which stay in Forge)."""

FOLLOW_UP = """This message continues an ongoing conversation about this project. Earlier in this conversation (oldest first):

{history}

The user's new message follows. Treat the earlier turns as context; the project's files may have changed since then, so read them again before relying on what a summary says.

{message}"""

TURN = """[Turn {number}] User: {prompt}
[Turn {number}] Result: {summary}"""


def follow_up(history: str, message: str) -> str:
    """A prompt that carries the conversation so far."""
    return FOLLOW_UP.format(history=history, message=message)


def turn(number: int, prompt: str, summary: str) -> str:
    """One earlier turn as it appears in the conversation so far."""
    return TURN.format(number=number, prompt=prompt, summary=summary)
