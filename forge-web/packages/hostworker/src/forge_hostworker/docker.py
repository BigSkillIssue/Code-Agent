"""The Docker command line, and the hardening every container of a hosted app gets.

Every container runs under gVisor (`--runtime=runsc`) with all capabilities dropped, no new
privileges and an unprivileged user. The worker refuses to start where runsc is missing.
"""

import asyncio
import json
from dataclasses import dataclass
from typing import Protocol

TAIL_CHARS = 20_000


@dataclass(frozen=True)
class DockerResult:
    """What one docker command printed and how it ended."""

    code: int
    out: str
    err: str

    def tail(self) -> str:
        """The end of what it printed."""
        text = "\n".join(t for t in (self.out.rstrip(), self.err.rstrip()) if t)
        return text[-TAIL_CHARS:]


class Docker(Protocol):
    """Runs `docker <args>`."""

    async def __call__(self, *args: str, timeout: float = 600) -> DockerResult: ...


class DockerCli:
    """The real docker binary."""

    def __init__(self, binary: str = "docker") -> None:
        self.binary = binary

    async def __call__(self, *args: str, timeout: float = 600) -> DockerResult:
        """Run docker; a timeout kills it and is exit 124."""
        proc = await asyncio.create_subprocess_exec(
            self.binary,
            *args,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            out, err = await asyncio.wait_for(proc.communicate(), timeout)
        except TimeoutError:
            proc.kill()
            await proc.wait()
            return DockerResult(124, "", f"docker {args[0]} took longer than {timeout:.0f}s")
        code = proc.returncode if proc.returncode is not None else 1
        return DockerResult(code, out.decode(errors="replace"), err.decode(errors="replace"))


class HostError(Exception):
    """The host cannot run apps (no Docker, no runsc); `hint` says what to do."""

    def __init__(self, message: str, hint: str) -> None:
        super().__init__(message)
        self.hint = hint


def hardened(user: str) -> list[str]:
    """The flags every app and build container gets."""
    return [
        "--runtime=runsc",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges",
        "--user",
        user,
    ]


async def require_runsc(docker: Docker) -> None:
    """Refuse to run without gVisor: the apps' code is foreign code."""
    found = await docker("info", "--format", "{{json .Runtimes}}", timeout=60)
    if found.code != 0:
        raise HostError(
            f"Docker does not answer: {found.tail()[-300:]}",
            hint="start Docker, and add the worker's user to the docker group",
        )
    try:
        runtimes = json.loads(found.out or "{}")
    except json.JSONDecodeError:
        runtimes = {}
    if not isinstance(runtimes, dict) or "runsc" not in runtimes:
        raise HostError(
            "gVisor (runsc) is not a Docker runtime on this host",
            hint="install gVisor and register it (`sudo runsc install`, then restart Docker); "
            "see docs/EINRICHTUNG.md §11",
        )
