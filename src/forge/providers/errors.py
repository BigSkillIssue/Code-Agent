"""Map HTTP error responses from any vendor to ProviderError kinds."""

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


def error_from_status(status: int, body: str, headers: Mapping[str, str]) -> ProviderError:
    """Map an HTTP error response to a ProviderError kind."""
    lowered = body.lower()
    message = f"HTTP {status}: {body[:500]}"
    kind: ErrorKind
    if status in (401, 403) or "insufficient_quota" in lowered:
        kind = "auth"
    elif status == 429:
        return ProviderError("rate_limit", message, retry_after(headers))
    elif status in (408, 425):
        kind = "network"
    elif status >= 500:
        kind = "overloaded"
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
