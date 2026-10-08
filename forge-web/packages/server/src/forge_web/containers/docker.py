"""Docker isolation: one hardened container per project, driven through the docker CLI.

The container's main process is the sandbox daemon; the server reaches it with
`docker exec -i -u 0 <container> … attach`, so the daemon (and every chat in it) keeps running
when the server restarts. The project's files and its home folder are named volumes, never
folders of the host.
"""

import asyncio
import contextlib
import json
import logging
import secrets
import shutil
from typing import Any

from forge_sandbox.protocol import PROTOCOL_VERSION
from forge_web.containers.driver import SandboxError, SandboxLink, check_project_id
from forge_web.containers.gitjob import SCRIPT, GitJob, run_job
from forge_web.settings import SandboxSettings

log = logging.getLogger(__name__)
PYTHON = "/opt/forge/bin/python"
SOCKET = "/run/forge-sandbox/daemon.sock"
LABEL = "org.forge-web.project"
# Capabilities the daemon needs to start programs as the project user and to manage that
# user's files; everything else is dropped.
CAPABILITIES = ("CHOWN", "DAC_OVERRIDE", "FOWNER", "SETUID", "SETGID", "KILL")
PROJECT_USER = "1000:1000"  # the image's forge user, who owns the workspace


class DockerDriver:
    """Starts, connects to, stops and removes project containers."""

    name = "docker"

    def __init__(self, settings: SandboxSettings, *, egress_port: int | None = None) -> None:
        self.settings = settings
        self.egress_port = egress_port  # programs reach the internet only through this proxy
        self.binary = shutil.which(settings.docker) or settings.docker
        self._runtime: str | None = None
        self._locks: dict[str, asyncio.Lock] = {}

    def container(self, project_id: str) -> str:
        """The container's name."""
        return f"forge-web-{check_project_id(project_id)}"

    def volumes(self, project_id: str) -> tuple[str, str]:
        """The workspace and home volume names."""
        base = self.container(project_id)
        return f"{base}-workspace", f"{base}-home"

    def run_args(self, project_id: str, runtime: str | None) -> list[str]:
        """`docker run` arguments of a project's container."""
        s = self.settings
        workspace, home = self.volumes(project_id)
        args = [
            # --pull never: a missing image is an error, never something fetched from a registry.
            "run", "--detach", "--init", "--pull", "never", "--name", self.container(project_id),
            "--label", f"{LABEL}={project_id}",
            "--network", "none",
            "--read-only",
            "--tmpfs", "/tmp:rw,nosuid,nodev,size=1g",
            "--tmpfs", "/run/forge-sandbox:rw,nosuid,nodev,noexec,mode=0700,size=1m",
            "--mount", f"type=volume,source={workspace},target=/workspace",
            "--mount", f"type=volume,source={home},target=/home/forge",
            "--cap-drop", "ALL",
            *[arg for cap in CAPABILITIES for arg in ("--cap-add", cap)],
            "--security-opt", "no-new-privileges",
            "--cpus", str(s.cpus),
            "--memory", s.memory,
            "--pids-limit", str(s.pids),
            "--ulimit", f"nofile={s.nofile}:{s.nofile}",
            "--restart", "no",
        ]  # fmt: skip
        if runtime:
            args += ["--runtime", runtime]
        if self.egress_port is not None:
            proxy = f"http://127.0.0.1:{self.egress_port}"
            for name in ("HTTPS_PROXY", "HTTP_PROXY", "https_proxy", "http_proxy"):
                args += ["--env", f"{name}={proxy}"]
            args += [
                "--env",
                "NO_PROXY=127.0.0.1,localhost",
                "--env",
                "no_proxy=127.0.0.1,localhost",
            ]
        return [*args, s.image]

    async def docker(
        self, *args: str, timeout: float = 120, check: bool = False
    ) -> tuple[int, str, str]:
        """Run one docker command; (exit code, stdout, stderr)."""
        try:
            proc = await asyncio.create_subprocess_exec(
                self.binary, *args,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )  # fmt: skip
        except OSError as error:
            raise SandboxError(f"cannot run {self.settings.docker}: {error.strerror}") from None
        try:
            out, errors = await asyncio.wait_for(proc.communicate(), timeout)
        except TimeoutError:
            proc.kill()
            raise SandboxError(f"docker {args[0]} took longer than {timeout:.0f} s") from None
        code = proc.returncode if proc.returncode is not None else -1
        result = (code, out.decode("utf-8", "replace"), errors.decode("utf-8", "replace"))
        if check and code != 0:
            raise SandboxError(f"docker {args[0]} failed: {result[2].strip()[:400]}")
        return result

    async def runtime(self) -> str | None:
        """The OCI runtime to use: runsc when asked for or (auto) when installed."""
        if self.settings.runtime == "runc":
            return None
        if self._runtime is None:
            code, out, _ = await self.docker("info", "--format", "{{json .Runtimes}}", timeout=30)
            runtimes = json.loads(out) if code == 0 and out.strip() else {}
            self._runtime = "runsc" if "runsc" in runtimes else ""
        if self.settings.runtime == "runsc" and not self._runtime:
            raise SandboxError("the runsc (gVisor) runtime is not installed in docker")
        return self._runtime or None

    async def inspect(self, project_id: str) -> dict[str, Any] | None:
        """The container's inspect data, or None if it does not exist."""
        code, out, _ = await self.docker("inspect", self.container(project_id), timeout=30)
        if code != 0:
            return None
        data = json.loads(out)
        return data[0] if isinstance(data, list) and data else None

    async def ensure(self, project_id: str) -> None:
        """Make sure the project's container runs the current protocol."""
        lock = self._locks.setdefault(project_id, asyncio.Lock())
        async with lock:
            info = await self.inspect(project_id)
            labels = (info or {}).get("Config", {}).get("Labels") or {}
            if info is not None and labels.get("org.forge-web.protocol") != str(PROTOCOL_VERSION):
                log.info(
                    "recreating %s for protocol %s", self.container(project_id), PROTOCOL_VERSION
                )
                await self.docker("rm", "--force", self.container(project_id), check=True)
                info = None
            if info is None:
                runtime = await self.runtime()
                await self.docker(*self.run_args(project_id, runtime), timeout=300, check=True)
            elif not info.get("State", {}).get("Running"):
                await self.docker("start", self.container(project_id), check=True)
            await self._wait_for_socket(project_id)

    async def _wait_for_socket(self, project_id: str, seconds: float = 30) -> None:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + seconds
        while loop.time() < deadline:
            code, _, _ = await self.docker(
                "exec", "-u", "0", self.container(project_id), "test", "-S", SOCKET, timeout=30
            )
            if code == 0:
                return
            await asyncio.sleep(0.2)
        raise SandboxError(f"the sandbox of project {project_id} did not start")

    async def connect(self, project_id: str) -> SandboxLink:
        """A new stream to the project's daemon (starting the container if needed)."""
        await self.ensure(project_id)
        proc = await asyncio.create_subprocess_exec(
            self.binary, "exec", "-i", "-u", "0", self.container(project_id),
            PYTHON, "-I", "-m", "forge_sandbox", "attach", "--socket", SOCKET,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            limit=2**20,
        )  # fmt: skip
        assert proc.stdin is not None and proc.stdout is not None

        async def close() -> None:
            with contextlib.suppress(Exception):
                proc.stdin.close()  # type: ignore[union-attr]
            try:
                await asyncio.wait_for(proc.wait(), 5)
            except TimeoutError:
                proc.kill()

        return SandboxLink(proc.stdout, proc.stdin, close)

    async def stop(self, project_id: str) -> None:
        """Stop the container (volumes stay; chats and programs in it end)."""
        await self.docker("stop", "--time", "10", self.container(project_id), timeout=60)

    async def remove(self, project_id: str) -> None:
        """Remove the container and both volumes."""
        await self.docker("rm", "--force", self.container(project_id), timeout=60)
        for volume in self.volumes(project_id):
            await self.docker("volume", "rm", "--force", volume, timeout=60)

    def git_job_args(
        self, project_id: str, job: GitJob, name: str, runtime: str | None
    ) -> list[str]:
        """`docker run` arguments of a git job: a throwaway, hardened container with the
        workspace volume and network access, running only the job script."""
        workspace, _ = self.volumes(project_id)
        args = [
            "run", "--rm", "-i", "--pull", "never", "--name", name,
            "--label", f"{LABEL}.gitjob={project_id}",
            "--user", PROJECT_USER, "--read-only",
            "--tmpfs", "/tmp:rw,nosuid,nodev,size=1g",
            "--mount", f"type=volume,source={workspace},target=/workspace",
            "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
            "--pids-limit", "256", "--memory", "1g", "--cpus", "1",
            "--entrypoint", "sh",
        ]  # fmt: skip
        if runtime:
            args += ["--runtime", runtime]
        if job.pin is not None:
            args += ["--add-host", f"{job.pin[0]}:{job.pin[1]}"]
        for key, value in job.env("/workspace", "/tmp/job").items():
            args += ["--env", f"{key}={value}"]
        return [*args, self.settings.image, "-c", SCRIPT]

    async def run_git_job(self, project_id: str, job: GitJob) -> tuple[int, str]:
        """Run a git job in its own container (removed afterwards, also on timeout)."""
        name = f"{self.container(project_id)}-git-{secrets.token_hex(4)}"
        args = self.git_job_args(project_id, job, name, await self.runtime())
        code, output = await run_job([self.binary, *args], None, job.stdin(), job.timeout)
        if code == -1:
            await self.docker("rm", "--force", name, timeout=60)
        return code, output
