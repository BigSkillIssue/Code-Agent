"""Map HTTP error responses from any vendor to ProviderError kinds."""

import re
from collections.abc import Mapping

from forge.providers.base import ErrorKind, ProviderError

OVERFLOW_HINTS = (
    "context_length",
    "context length",
    "maximum context",
    "context window",
    "too many tokens",
    "prompt is too long",
)
# Some vendors (Gemini's free tier) say how long to wait only in the body.
BODY_RETRY = re.compile(
    r'(?:retry|try again) in ([0-9.]+)\s*s|"retryDelay":\s*"([0-9.]+)s"', re.IGNORECASE
)
PER_MINUTE_QUOTA = ("tokens per minute", "rate_limit_exceeded")


def error_from_status(status: int, body: str, headers: Mapping[str, str]) -> ProviderError:
    """Map an HTTP error response to a ProviderError kind."""
    lowered = body.lower()
    message = f"HTTP {status}: {body[:500]}"
    kind: ErrorKind
    if status in (401, 403) or "insufficient_quota" in lowered:
        kind = "auth"
    elif status == 429:
        wait = retry_after(headers)
        return ProviderError("rate_limit", message, wait if wait is not None else body_wait(body))
    elif status in (408, 425):
        kind = "network"
    elif status >= 500:
        kind = "overloaded"
    elif status == 413 and any(hint in lowered for hint in PER_MINUTE_QUOTA):
        # One request above the per-minute quota (Groq's free tier): waiting cannot help.
        return ProviderError(
            "bad_request", f"request exceeds the provider's per-minute token limit: {message}"
        )
    elif status == 413 or any(hint in lowered for hint in OVERFLOW_HINTS):
        kind = "context_overflow"
    else:
        kind = "bad_request"
    return ProviderError(kind, message)


def retry_after(headers: Mapping[str, str]) -> float | None:
    """Seconds to wait, from `retry-after-ms` or `retry-after` (numeric form only)."""
    for name, scale in (("retry-after-ms", 0.001), ("retry-after", 1.0)):
        value = headers.get(name)
        if value is not None:
            try:
                return float(value) * scale
            except ValueError:
                return None
    return None


def body_wait(body: str) -> float | None:
    """Seconds to wait, as written in an error body ("Please retry in 44.5s")."""
    match = BODY_RETRY.search(body)
    if match is None:
        return None
    return float(match.group(1) or match.group(2))
