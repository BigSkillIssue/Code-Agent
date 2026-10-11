"""The Docker command line, and the hardening every container of a hosted app gets.

Every container runs under gVisor (`--runtime=runsc`) with all capabilities dropped, no new
privileges and an unprivileged user. The worker refuses to start where runsc is missing.
"""

import asyncio
import contextlib
import json
from dataclasses import dataclass
from pathlib import Path
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
    """Runs `docker <args>`; with `out`, what it prints goes into that file instead."""

    async def __call__(
        self, *args: str, timeout: float = 600, out: Path | None = None
    ) -> DockerResult: ...


class DockerCli:
    """The real docker binary."""

    def __init__(self, binary: str = "docker") -> None:
        self.binary = binary

    async def __call__(
        self, *args: str, timeout: float = 600, out: Path | None = None
    ) -> DockerResult:
        """Run docker; a timeout kills it and is exit 124."""
        with contextlib.ExitStack() as files:
            target = files.enter_context(out.open("wb")) if out else asyncio.subprocess.PIPE
            proc = await asyncio.create_subprocess_exec(
                self.binary,
                *args,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=target,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                printed, err = await asyncio.wait_for(proc.communicate(), timeout)
            except TimeoutError:
                proc.kill()
                await proc.wait()
                return DockerResult(124, "", f"docker {args[0]} took longer than {timeout:.0f}s")
        code = proc.returncode if proc.returncode is not None else 1
        text = printed.decode(errors="replace") if printed else ""
        return DockerResult(code, text, err.decode(errors="replace"))


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


async def uses_userns(docker: Docker) -> bool:
    """Whether Docker remaps container users to the host's subordinate ids (`userns-remap`)."""
    found = await docker("info", "--format", "{{json .SecurityOptions}}", timeout=60)
    return found.code == 0 and "name=userns" in found.out


def mapped_id(container_id: int, subids: str, name: str = "dockremap") -> int:
    """The host id a container id becomes under userns-remap (`subids`: /etc/subuid's text)."""
    for line in subids.splitlines():
        parts = line.strip().split(":")
        numbers = len(parts) == 3 and parts[1].isdigit() and parts[2].isdigit()
        if numbers and parts[0] == name and container_id < int(parts[2]):
            return int(parts[1]) + container_id
    raise HostError(
        f"/etc/subuid or /etc/subgid has no range for {name}",
        hint='set "userns-remap": "default" in /etc/docker/daemon.json and restart Docker',
    )


async def container_owner(
    docker: Docker, user: str, root: bool, etc: Path = Path("/etc")
) -> tuple[int, int] | None:
    """The host uid and gid of the containers' user, when the worker (root) must hand release
    folders to it; None when the folders are already the worker's own (it is that user)."""
    userns = await uses_userns(docker)
    if not root:
        if userns:
            raise HostError(
                "Docker remaps users (userns-remap), so the worker must run as root",
                hint="start it with deploy/host/forge-host-worker.service",
            )
        return None
    uid, gid = (int(part) for part in user.split(":"))
    if not userns:
        return uid, gid
    subuid = (etc / "subuid").read_text(encoding="utf-8")
    subgid = (etc / "subgid").read_text(encoding="utf-8")
    return mapped_id(uid, subuid), mapped_id(gid, subgid)
