"""Mapping vendor error responses to ProviderError kinds (cases seen in live runs)."""

from forge.providers.errors import error_from_status

GROQ_TPM_429 = (
    '{"error":{"message":"Rate limit reached for model `openai/gpt-oss-120b` in organization '
    "`org_X` service tier `on_demand` on tokens per minute (TPM): Limit 8000, Used 6183, "
    'Requested 3358. Please try again in 11.5575s.","type":"tokens","code":"rate_limit_exceeded"}}'
)
GROQ_TPM_413 = (
    '{"error":{"message":"Request too large for model `openai/gpt-oss-120b` in organization '
    "`org_X` service tier `on_demand` on tokens per minute (TPM): Limit 8000, Requested 8713, "
    'please reduce your message size and try again.","type":"tokens","code":"rate_limit_exceeded"}}'
)


def test_wait_from_try_again_in_the_body() -> None:
    err = error_from_status(429, GROQ_TPM_429, {})
    assert err.kind == "rate_limit" and err.retry_after_s == 11.5575


def test_a_header_wait_wins_over_the_body() -> None:
    assert error_from_status(429, GROQ_TPM_429, {"retry-after": "3"}).retry_after_s == 3.0


def test_request_above_the_per_minute_quota_is_not_a_context_overflow() -> None:
    """Waiting cannot help and the model's context is not full: try the next model at once."""
    err = error_from_status(413, GROQ_TPM_413, {})
    assert err.kind == "bad_request" and "per-minute token limit" in str(err)


def test_a_real_413_is_still_a_context_overflow() -> None:
    assert error_from_status(413, "Request entity too large", {}).kind == "context_overflow"
