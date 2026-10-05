"""Mask secrets in tool output before the model (or a spill file) sees them.

Two kinds are masked: values that look like well-known API keys or private keys, and the
values of environment variables whose names say they are secret (`*_TOKEN`, `*_KEY`, ...).
"""

import os
import re
from collections.abc import Mapping

MASK = "[masked secret]"
KEY_PATTERNS = re.compile(
    "|".join(
        [
            r"sk-ant-[A-Za-z0-9_-]{20,}",  # Anthropic
            r"sk-(?:proj-|live-|test-)?[A-Za-z0-9_-]{32,}",  # OpenAI and similar
            r"gh[pousr]_[A-Za-z0-9]{30,}",  # GitHub tokens
            r"github_pat_[A-Za-z0-9_]{40,}",
            r"(?:AKIA|ASIA)[0-9A-Z]{16}",  # AWS access key ids
            r"xox[abposr]-[A-Za-z0-9-]{20,}",  # Slack
            r"AIza[0-9A-Za-z_-]{35}",  # Google API keys
            r"glpat-[A-Za-z0-9_-]{20,}",  # GitLab
            r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----",
        ]
    )
)
SECRET_NAME = re.compile(r"KEY|TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIAL|AUTH", re.IGNORECASE)
MIN_SECRET_CHARS = 8


def secret_values(environ: Mapping[str, str]) -> list[str]:
    """Values of secret-named environment variables, longest first."""
    values = {v for k, v in environ.items() if SECRET_NAME.search(k) and len(v) >= MIN_SECRET_CHARS}
    return sorted(values, key=len, reverse=True)


def mask_secrets(text: str, environ: Mapping[str, str] | None = None) -> str:
    """The text with key-like values and secret environment values replaced by MASK."""
    masked = KEY_PATTERNS.sub(MASK, text)
    for value in secret_values(os.environ if environ is None else environ):
        masked = masked.replace(value, MASK)
    return masked
