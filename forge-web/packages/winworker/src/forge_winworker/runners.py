"""Where a job's commands run.

`DirectRunner` runs them on this Windows machine itself, in the job's own folder, as the user the
worker runs as, without the worker's token in their environment. The products' code is foreign
code, so a build machine must hold nothing else: a VM of its own that is reset to a clean
snapshot now and then (see `forge-win-worker run --help`); a signing machine never runs it.
Builds in a throwaway Windows Sandbox per job may come later.
"""

import asyncio
import contextlib
import os
import shutil
from pathlib import Path
from typing import Protocol

QUIET = {"DOTNET_CLI_TELEMETRY_OPTOUT": "1", "DOTNET_NOLOGO": "1",
         "KIOTA_TUTORIAL_ENABLED": "false"}  # fmt: skip
WORKER_ONLY = ("WIN_WORKER_TOKEN",)  # the worker's own: never in a job's environment


def job_environment() -> dict[str, str]:
    """The environment a job's commands get: the worker's, without its token."""
    kept = {name: value for name, value in os.environ.items() if name.upper() not in WORKER_ONLY}
    return kept | QUIET


MAX_OUTPUT = 2_000_000  # characters of a command's output kept


class Runner(Protocol):
    """Gives each job a folder and runs its commands."""

    async def prepare(self, job_id: str) -> Path:
        """A fresh, empty folder for the job."""
        ...

    async def run(self, command: list[str], cwd: Path) -> tuple[int, str]:
        """Run a command; its exit code and output (stdout and stderr together)."""
        ...

    async def remove(self, folder: Path) -> None:
        """Delete a finished job's folder."""
        ...


class DirectRunner:
    """Runs the commands on this machine, each job in a folder of its own under `work`."""

    def __init__(self, work: Path) -> None:
        self.work = work

    async def prepare(self, job_id: str) -> Path:
        folder = self.work / "jobs" / job_id
        await asyncio.to_thread(shutil.rmtree, folder, True)
        folder.mkdir(parents=True)
        return folder

    async def run(self, command: list[str], cwd: Path) -> tuple[int, str]:
        process = await asyncio.create_subprocess_exec(
            *command, cwd=cwd, env=job_environment(),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
        )  # fmt: skip
        try:
            output, _ = await process.communicate()
        except asyncio.CancelledError:  # the job ran out of time
            with contextlib.suppress(ProcessLookupError):
                process.kill()
            raise
        text = output.decode("utf-8", "replace")[-MAX_OUTPUT:]
        return process.returncode or 0, text

    async def remove(self, folder: Path) -> None:
        await asyncio.to_thread(shutil.rmtree, folder, True)
