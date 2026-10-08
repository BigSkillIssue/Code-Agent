"""Local isolation: the sandbox daemon runs as a child process on this machine.

Meant for development, tests and single-person installs: programs run as the server's user
(inside Forge's own OS sandbox where available), not in a container.
"""

import asyncio
import contextlib
import os
import shutil
import sys
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from forge.local.local_executor import stop_tree
from forge.runtime.shell import new_process_group

from forge_web.containers.driver import SandboxError, SandboxLink, check_project_id
from forge_web.containers.gitjob import SCRIPT, GitJob, run_job

# What a sandbox inherits from the server's environment: nothing secret, just enough to run.
PASS_ENV = (
    "PATH", "HOME", "USER", "LOGNAME", "LANG", "LC_ALL", "LC_CTYPE", "TZ", "TMPDIR", "TEMP",
    "TMP", "SYSTEMROOT", "WINDIR", "COMSPEC", "PATHEXT", "USERPROFILE", "APPDATA",
    "LOCALAPPDATA", "PROGRAMDATA", "PROGRAMFILES", "FORGE_BASH", "FORGE_POWERSHELL",
)  # fmt: skip


def process_group() -> dict[str, Any]:
    """subprocess arguments that start the daemon in its own process group."""
    return dict(new_process_group())


def sandbox_env(forge_home: Path) -> dict[str, str]:
    """The environment of a local sandbox daemon."""
    env = {name: os.environ[name] for name in PASS_ENV if name in os.environ}
    env["FORGE_HOME"] = str(forge_home)
    env["PYTHONUNBUFFERED"] = "1"
    return env


class LocalDriver:
    """Runs `forge-sandbox daemon --stdio` per connection, with the project under data_dir."""

    name = "local"

    def __init__(
        self,
        data_dir: Path,
        *,
        python: str = sys.executable,
        folders: Mapping[str, Path] | None = None,
    ) -> None:
        self.projects = data_dir / "projects"
        self.python = python
        self.folders: dict[str, Path] = dict(folders or {})  # project id -> a server folder
        self._running: dict[str, set[asyncio.subprocess.Process]] = {}

    def project_dir(self, project_id: str) -> Path:
        """The folder that holds a project's workspace, Forge home and logs."""
        return self.projects / check_project_id(project_id)

    def workspace(self, project_id: str) -> Path:
        """The project's files (an existing folder for server-folder projects)."""
        folder = self.folders.get(project_id)
        return folder if folder is not None else self.project_dir(project_id) / "workspace"

    async def connect(self, project_id: str) -> SandboxLink:
        """Start a daemon for the project and return its stdin/stdout."""
        base = self.project_dir(project_id)
        workspace = self.workspace(project_id)
        for folder in (workspace, base / "forge-home"):
            folder.mkdir(parents=True, exist_ok=True)
        log = (base / "daemon.log").open("ab")
        try:
            proc = await asyncio.create_subprocess_exec(
                self.python, "-I", "-m", "forge_sandbox", "daemon", "--stdio",
                "--workspace", str(workspace),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=log,
                env=sandbox_env(base / "forge-home"),
                limit=2**20,
                **process_group(),
            )  # fmt: skip
        except OSError as err:
            raise SandboxError(f"could not start the sandbox daemon: {err.strerror}") from None
        finally:
            log.close()
        assert proc.stdin is not None and proc.stdout is not None
        self._running.setdefault(project_id, set()).add(proc)

        async def close() -> None:
            self._running.get(project_id, set()).discard(proc)
            if proc.stdin is not None:
                with contextlib.suppress(Exception):
                    proc.stdin.close()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(proc.wait(), 3)
            await stop_tree(proc)

        return SandboxLink(proc.stdout, proc.stdin, close)

    async def stop(self, project_id: str) -> None:
        """Stop every daemon of the project."""
        for proc in list(self._running.pop(check_project_id(project_id), set())):
            await stop_tree(proc)

    async def remove(self, project_id: str) -> None:
        """Stop the daemons and delete the project's folder (never a server folder)."""
        await self.stop(project_id)
        self.folders.pop(project_id, None)
        await asyncio.to_thread(shutil.rmtree, self.project_dir(project_id), True)

    async def run_git_job(self, project_id: str, job: GitJob) -> tuple[int, str]:
        """Run a git job as a child process, in a temporary folder that is removed afterwards."""
        shell = shutil.which("sh")
        if shell is None:
            raise SandboxError("pushing and pulling need a POSIX shell (sh) in local mode")
        workspace = self.workspace(check_project_id(project_id))
        with tempfile.TemporaryDirectory(prefix="forge-web-git-") as tmp:
            base = {name: os.environ[name] for name in ("PATH", "SYSTEMROOT") if name in os.environ}
            env = {**base, **job.env(str(workspace), tmp)}
            return await run_job([shell, "-c", SCRIPT], env, job.stdin(), job.timeout)
