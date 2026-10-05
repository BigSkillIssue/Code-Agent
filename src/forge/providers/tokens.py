"""Rough token counts for providers without a counting endpoint (about 4 characters each)."""

import json

from forge.providers.base import ChatRequest, ImagePart, Message

CHARS_PER_TOKEN = 4
IMAGE_TOKENS = 1_000


def message_tokens(message: Message) -> int:
    """Estimated tokens of one message."""
    chars = sum(len(p.text) for p in message.parts if not isinstance(p, ImagePart))
    images = sum(1 for p in message.parts if isinstance(p, ImagePart))
    chars += sum(len(c.name) + len(json.dumps(c.arguments)) for c in message.tool_calls)
    if message.tool_result:
        chars += len(message.tool_result.text)
        images += len(message.tool_result.images)
    return chars // CHARS_PER_TOKEN + images * IMAGE_TOKENS + 4


def estimate_tokens(req: ChatRequest) -> int:
    """Estimated input tokens of a whole request: system, messages and tool schemas."""
    chars = len(req.system) + sum(len(json.dumps(t.model_dump())) for t in req.tools)
    return chars // CHARS_PER_TOKEN + sum(message_tokens(m) for m in req.messages)
