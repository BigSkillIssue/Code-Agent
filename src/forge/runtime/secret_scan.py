"""Find secrets written into a product's files (S65): keys, tokens and private key files that
belong in Forge Web's vault, not in the repository. Findings name the file, the line and the
kind of secret, never the secret itself.
"""

import re
from dataclasses import dataclass
from pathlib import Path

from forge.runtime.ignore import project_files

MAX_BYTES = 1_000_000
LOCKFILES = frozenset({"uv.lock", "package-lock.json", "poetry.lock", "yarn.lock"})
SECRET_FILES = (
    (re.compile(r"^\.env(\..+)?$"), "an environment file"),
    (re.compile(r"\.(pem|p8|p12|pfx|key)$"), "a key or certificate file"),
    (re.compile(r"^id_(rsa|ecdsa|ed25519)$"), "an SSH private key"),
)
SAFE_FILES = re.compile(r"^\.env\.(example|sample|template)$")
# A URL with a real password; local development databases (localhost, compose's db) are fine.
DATABASE_URL = re.compile(
    r"(?i)(?:postgres(?:ql)?|mysql|mongodb(?:\+srv)?)://[^:/\s]+:[^@\s]{6,}@"
    r"(?!localhost|127\.0\.0\.1|db[:/]|postgres[:/])"
)
PATTERNS = (
    (re.compile(r"-----BEGIN (?:[A-Z]+ )?PRIVATE KEY-----"), "a private key"),
    (re.compile(r"(?<![A-Za-z0-9])AKIA[0-9A-Z]{16}(?![A-Za-z0-9])"), "an AWS access key"),
    (re.compile(r"(?<![A-Za-z0-9])[sr]k_live_[0-9A-Za-z]{16,}"), "a Stripe live key"),
    (re.compile(r"(?<![A-Za-z0-9])sk_test_[0-9A-Za-z]{16,}"), "a Stripe test key"),
    (re.compile(r"(?<![A-Za-z0-9])whsec_[0-9A-Za-z]{20,}"), "a Stripe webhook secret"),
    (re.compile(r"(?<![A-Za-z0-9])gh[pousr]_[A-Za-z0-9]{36,}"), "a GitHub token"),
    (re.compile(r"(?<![A-Za-z0-9])github_pat_[A-Za-z0-9_]{50,}"), "a GitHub token"),
    (re.compile(r"(?<![A-Za-z0-9])xox[abprs]-[A-Za-z0-9-]{10,}"), "a Slack token"),
    (re.compile(r"(?<![A-Za-z0-9])AIza[0-9A-Za-z_-]{35}"), "a Google API key"),
    (re.compile(r"(?<![A-Za-z0-9])sk-ant-[A-Za-z0-9_-]{20,}"), "an Anthropic API key"),
    (re.compile(r"(?<![A-Za-z0-9-])sk-(?:proj-)?[A-Za-z0-9_-]{40,}"), "an OpenAI API key"),
    (DATABASE_URL, "a database URL with a password"),
)


@dataclass(frozen=True)
class SecretFinding:
    """One secret: where it is and what kind it is."""

    path: str  # relative to the project, with /
    line: int  # 0 for a whole file
    kind: str


async def scan_secrets(root: Path) -> list[SecretFinding]:
    """Secrets in the project's files (tracked and untracked, ignored files left out)."""
    findings: list[SecretFinding] = []
    for path in await project_files(root, hidden=True):
        rel = path.relative_to(root).as_posix()
        findings += _secret_file(rel, path.name)
        if path.name in LOCKFILES or path.stat().st_size > MAX_BYTES:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue  # binary or unreadable: only its name is checked
        findings += scan_text(rel, text)
    return findings


def _secret_file(rel: str, name: str) -> list[SecretFinding]:
    """A finding when the file itself is a secret (.env, a private key)."""
    if SAFE_FILES.match(name):
        return []
    return [SecretFinding(rel, 0, kind) for pattern, kind in SECRET_FILES if pattern.search(name)]


def scan_text(rel: str, text: str) -> list[SecretFinding]:
    """Secrets in one file's text, one finding per line and kind."""
    findings: list[SecretFinding] = []
    for number, line in enumerate(text.splitlines(), start=1):
        for pattern, kind in PATTERNS:
            if pattern.search(line):
                findings.append(SecretFinding(rel, number, kind))
    return findings
