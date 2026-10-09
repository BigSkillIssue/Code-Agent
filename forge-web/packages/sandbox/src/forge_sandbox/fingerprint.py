"""Which Forge a process runs: a short hash of the Forge package's Python files.

The server, every sandbox (in its hello) and `forge-web doctor` compute it the same way, from a
checkout or from an installed wheel, so a sandbox image built from another Forge than the
server's shows up instead of going unnoticed.
"""

import hashlib
from pathlib import Path

import forge


def fingerprint_of(package: Path) -> str:
    """A short hash over a package's Python files: their paths and their contents."""
    digest = hashlib.sha256()
    for path in sorted(package.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        digest.update(path.relative_to(package).as_posix().encode() + b"\0")
        digest.update(path.read_bytes() + b"\0")
    return digest.hexdigest()[:12]


def forge_fingerprint() -> str:
    """The fingerprint of the Forge this process imports."""
    return fingerprint_of(Path(forge.__file__).parent)
