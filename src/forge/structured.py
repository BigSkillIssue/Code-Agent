"""Read structured answers (JSON objects) out of model text."""

import json
import re
from typing import Any

from pydantic import BaseModel, ValidationError

_FENCE = re.compile(r"```(?:json)?\s*\n(.*?)```", re.DOTALL)


class StructuredError(ValueError):
    """The model's answer did not contain the expected JSON."""


def json_object(text: str) -> dict[str, Any]:
    """The first JSON object in `text`: raw, inside a ```json fence, or embedded in prose."""
    candidates = [text.strip(), *(m.strip() for m in _FENCE.findall(text))]
    start, end = text.find("{"), text.rfind("}")
    if 0 <= start < end:
        candidates.append(text[start : end + 1])
    for candidate in candidates:
        try:
            value = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            return value
    raise StructuredError("no JSON object found in the answer")


def parse_as[M: BaseModel](text: str, model: type[M]) -> M:
    """Parse `text` into `model`; StructuredError explains what was wrong."""
    try:
        return model.model_validate(json_object(text))
    except ValidationError as exc:
        problems = "; ".join(f"{'.'.join(map(str, e['loc']))}: {e['msg']}" for e in exc.errors())
        raise StructuredError(f"the JSON does not match the schema: {problems}") from exc
