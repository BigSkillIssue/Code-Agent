"""Run trusted helper programs (git, rg, pdftotext) outside the command sandbox.

Commands the model asks for go through the Executor port instead.
"""

import asyncio
import os
import shutil
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

QUIET_ENV = {"NO_COLOR": "1", "TERM": "dumb", "CI": "1", "PAGER": "cat", "GIT_PAGER": "cat"}


@dataclass
class ProcResult:
    """Exit code and decoded output of a helper program."""

    code: int
    stdout: str
    stderr: str


def which(program: str) -> str | None:
    """Full path of a program on PATH, or None."""
    return shutil.which(program)


async def run_argv(
    argv: Sequence[str],
    cwd: Path,
    *,
    timeout_s: float = 60,
    env: Mapping[str, str] | None = None,
) -> ProcResult:
    """Run a program to completion with stdin closed; returns code -1 on timeout."""
    proc = await asyncio.create_subprocess_exec(
        *argv,
        cwd=cwd,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env={**os.environ, **QUIET_ENV, **(env or {})},
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout_s)
    except TimeoutError:
        proc.kill()
        await proc.wait()
        return ProcResult(-1, "", f"timed out after {timeout_s:.0f}s")
    code = proc.returncode if proc.returncode is not None else -1
    return ProcResult(code, out.decode("utf-8", "replace"), err.decode("utf-8", "replace"))
