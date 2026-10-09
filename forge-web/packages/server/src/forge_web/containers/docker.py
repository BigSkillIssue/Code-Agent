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
import re
import secrets
import shutil
from pathlib import Path
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
VOLUME = re.compile(r"^forge-web-([a-z0-9][a-z0-9-]{0,63})-(?:workspace|home)$")
SIZE = re.compile(r"^([0-9]+(?:\.[0-9]+)?)\s*([kKMGTP]?)B$")
UNITS = {"": 1, "k": 1000, "K": 1000, "M": 1000**2, "G": 1000**3, "T": 1000**4, "P": 1000**5}
# Characters that would end a value inside `--mount`'s comma-separated options.
MOUNT_UNSAFE = frozenset(',"\n')


def size_bytes(text: str) -> int | None:
    """Bytes of a size as the docker CLI prints it ("3MB", "1.25GB", "0B")."""
    match = SIZE.match(text.strip())
    return int(float(match[1]) * UNITS[match[2]]) if match else None


class DockerDriver:
    """Starts, connects to, stops and removes project containers."""

    name = "docker"

    def __init__(self, settings: SandboxSettings, *, egress_port: int | None = None) -> None:
        self.settings = settings
        self.egress_port = egress_port  # programs reach the internet only through this proxy
        self.binary = shutil.which(settings.docker) or settings.docker
        self._runtime: str | None = None
        self._locks: dict[str, asyncio.Lock] = {}
        self.folders: dict[str, Path] = {}  # project id -> a server folder used as /workspace

    def workspace_mount(self, project_id: str) -> str:
        """The `--mount` value of the project's files: its volume, or its server folder."""
        folder = self.folders.get(project_id)
        if folder is not None:
            if MOUNT_UNSAFE & set(str(folder)):
                raise SandboxError("the project's folder has a comma or quote in its path")
            return f"type=bind,source={folder},target=/workspace"
        return f"type=volume,source={self.volumes(project_id)[0]},target=/workspace"

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
        _, home = self.volumes(project_id)
        args = [
            # --pull never: a missing image is an error, never something fetched from a registry.
            "run", "--detach", "--init", "--pull", "never", "--name", self.container(project_id),
            "--label", f"{LABEL}={project_id}",
            "--network", "none",
            "--read-only",
            "--tmpfs", "/tmp:rw,nosuid,nodev,size=1g",
            "--tmpfs", "/run/forge-sandbox:rw,nosuid,nodev,noexec,mode=0700,size=1m",
            "--mount", self.workspace_mount(project_id),
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
            running = bool((info or {}).get("State", {}).get("Running"))
            if info is not None and labels.get("org.forge-web.protocol") != str(PROTOCOL_VERSION):
                log.info(
                    "recreating %s for protocol %s", self.container(project_id), PROTOCOL_VERSION
                )
                await self.docker("rm", "--force", self.container(project_id), check=True)
                info = None
            elif info is not None and not running and await self._outdated(info):
                # A rebuilt image (a newer Forge) reaches the project when its sandbox starts
                # again; a running one keeps its chats until the idle stop. Volumes stay.
                log.info("updating %s to the current sandbox image", self.container(project_id))
                await self.docker("rm", "--force", self.container(project_id), check=True)
                info = None
            if info is None:
                runtime = await self.runtime()
                await self.docker(*self.run_args(project_id, runtime), timeout=300, check=True)
            elif not running:
                await self.docker("start", self.container(project_id), check=True)
            await self._wait_for_socket(project_id)

    async def _outdated(self, info: dict[str, Any]) -> bool:
        """The container was made from another image than the one its tag names now."""
        code, out, _ = await self.docker(
            "image", "inspect", "--format", "{{.Id}}", self.settings.image, timeout=30
        )
        return code == 0 and bool(out.strip()) and info.get("Image") != out.strip()

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

    async def disk_use(self) -> dict[str, int]:
        """Bytes of each project's volumes as Docker itself counts them (never the sandbox)."""
        code, out, err = await self.docker("system", "df", "-v", "--format", "json", timeout=600)
        if code != 0:
            log.warning("could not measure the projects' volumes: %s", err.strip()[:200])
            return {}
        try:
            volumes = json.loads(out).get("Volumes") or []
        except (ValueError, AttributeError):
            return {}
        found: dict[str, int] = {}
        for volume in volumes if isinstance(volumes, list) else []:
            match = VOLUME.match(str(volume.get("Name", ""))) if isinstance(volume, dict) else None
            size = size_bytes(str(volume.get("Size", ""))) if match else None
            if match and size is not None:
                found[match[1]] = found.get(match[1], 0) + size
        return found

    async def stop(self, project_id: str) -> None:
        """Stop the container (volumes stay; chats and programs in it end)."""
        await self.docker("stop", "--time", "10", self.container(project_id), timeout=60)

    async def remove(self, project_id: str) -> None:
        """Remove the container and its volumes (a server folder stays as it is)."""
        await self.docker("rm", "--force", self.container(project_id), timeout=60)
        workspace, home = self.volumes(project_id)
        volumes = [home] if self.folders.pop(project_id, None) is not None else [workspace, home]
        for volume in volumes:
            await self.docker("volume", "rm", "--force", volume, timeout=60)

    def git_job_args(
        self, project_id: str, job: GitJob, name: str, runtime: str | None
    ) -> list[str]:
        """`docker run` arguments of a git job: a throwaway, hardened container with the
        workspace volume and network access, running only the job script."""
        args = [
            "run", "--rm", "-i", "--pull", "never", "--name", name,
            "--label", f"{LABEL}.gitjob={project_id}",
            "--user", PROJECT_USER, "--read-only",
            "--tmpfs", "/tmp:rw,nosuid,nodev,size=1g",
            "--mount", self.workspace_mount(project_id),
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
