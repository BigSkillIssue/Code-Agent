"""The interface every sandbox driver implements, and what a connection to a sandbox is."""

import asyncio
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Protocol

from forge_sandbox.mux import Writer
from forge_web.containers.gitjob import GitJob

PROJECT_ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")


class SandboxError(Exception):
    """A sandbox could not be started or reached; the message says why."""


@dataclass
class SandboxLink:
    """A byte stream to one project's sandbox daemon."""

    reader: asyncio.StreamReader
    writer: Writer
    close: Callable[[], Awaitable[None]]


class ContainerDriver(Protocol):
    """Starts, connects to, stops and removes project sandboxes."""

    name: str

    async def connect(self, project_id: str) -> SandboxLink:
        """A new stream to the project's daemon, starting the sandbox if needed."""
        ...

    async def stop(self, project_id: str) -> None:
        """Stop the project's sandbox (its files stay)."""
        ...

    async def remove(self, project_id: str) -> None:
        """Stop the sandbox and delete everything that belongs to the project."""
        ...

    async def run_git_job(self, project_id: str, job: GitJob) -> tuple[int, str]:
        """Run a push or fetch outside the sandbox: exit code and output."""
        ...


def check_project_id(project_id: str) -> str:
    """A project id safe to use in paths and container names."""
    if not PROJECT_ID.match(project_id):
        raise SandboxError(f"invalid project id {project_id!r}")
    return project_id
