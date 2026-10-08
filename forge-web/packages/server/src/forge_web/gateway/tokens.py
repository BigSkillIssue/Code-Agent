"""Run tokens: what a chat's worker shows the gateway instead of an API key.

A token names its chat and the chat's token generation and is signed with a key derived from the
master secret, so it survives server restarts without being stored. The gateway accepts it only
while the chat is working on a turn; bumping the generation revokes it.
"""

import hmac

from forge_web.vault import sign

PREFIX = "fwg"
TOKEN_ENV = "FW_GATEWAY_TOKEN"  # not FORGE_*: Forge reads those as config overrides


def run_token(key: bytes, chat_id: str, generation: int) -> str:
    """The token of one chat generation."""
    body = f"{chat_id}.{generation}"
    return f"{PREFIX}.{body}.{sign(key, body)}"


def parse_token(key: bytes, token: str) -> tuple[str, int] | None:
    """(chat id, generation) of a genuine token, else None."""
    parts = token.split(".")
    if len(parts) != 4 or parts[0] != PREFIX or not parts[2].isdigit():
        return None
    body = f"{parts[1]}.{parts[2]}"
    if not hmac.compare_digest(sign(key, body), parts[3]):
        return None
    return parts[1], int(parts[2])
