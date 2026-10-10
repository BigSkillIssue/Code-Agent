"""Where a job runs: in a macOS VM of its project (Tart), or straight on this Mac (direct mode).

A VM is cloned from a prepared image when a project's first job comes, keeps the project's build
folder while its jobs keep coming, and is deleted after `idle_s` without a job. Projects never
share a VM. Apple's license allows two macOS VMs per Mac, so at most `slots` VMs run at once.
"""

import asyncio
import contextlib
import json
import logging
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from forge_macworker.export import export_archive
from forge_macworker.job import RESULT, BuilderFactory, Exporter, run_job, xcode_builder
from forge_macworker.wire import JobResult

log = logging.getLogger(__name__)
MOUNT = "/Volumes/My Shared Files/jobs"  # where Tart shows the shared folder inside the VM


class Runner(Protocol):
    """Gives each job a folder, runs it there, and cleans up."""

    async def prepare(self, project: str, job_id: str) -> Path: ...
    async def run(self, project: str, folder: Path) -> JobResult: ...
    async def reap(self) -> None: ...
    async def drop(self, project: str) -> None: ...  # its VM goes at once (one-off jobs)
    async def close(self) -> None: ...


class DirectRunner:
    """Runs jobs on this Mac itself: for CI and for a Mac that builds only trusted projects."""

    def __init__(
        self, work: Path, make_builder: BuilderFactory = xcode_builder,
        exporter: Exporter = export_archive,
    ) -> None:  # fmt: skip
        self.work = work
        self.make_builder = make_builder
        self.exporter = exporter
        self.locks: dict[str, asyncio.Lock] = {}

    async def prepare(self, project: str, job_id: str) -> Path:
        """A fresh folder for the job."""
        folder = self.work / "jobs" / job_id
        folder.mkdir(parents=True, exist_ok=True)
        return folder

    async def run(self, project: str, folder: Path) -> JobResult:
        """Run the job here, one at a time per project."""
        async with self.locks.setdefault(project, asyncio.Lock()):
            return await run_job(folder, self.work / "projects", self.make_builder, self.exporter)

    async def reap(self) -> None:
        """Nothing to stop."""

    async def drop(self, project: str) -> None:
        """Forget a one-off job's lock (nothing to stop)."""
        self.locks.pop(project, None)

    async def close(self) -> None:
        """Nothing keeps running."""


@dataclass
class Lease:
    """A running VM of one project."""

    name: str
    shared: Path  # the host folder the VM sees at MOUNT
    process: asyncio.subprocess.Process
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    last_used: float = field(default_factory=time.monotonic)


@dataclass
class TartSettings:
    """How VMs are made and run."""

    image: str  # a prepared image (forge-mac-worker prepare-image)
    work: Path  # host folders shared with the VMs
    slots: int = 2
    network: str = "softnet"  # softnet: internet only, not this Mac's network; or "nat"
    command: str = "/Users/admin/.local/bin/forge-mac-job"  # inside the VM
    idle_s: float = 600
    tart: str = "tart"
    boot_timeout_s: float = 300


class TartRunner:
    """Runs each project's jobs in a VM of its own."""

    def __init__(self, settings: TartSettings) -> None:
        self.settings = settings
        self.leases: dict[str, Lease] = {}
        self.vms = asyncio.Semaphore(settings.slots)
        self.starting: dict[str, asyncio.Lock] = {}

    async def prepare(self, project: str, job_id: str) -> Path:
        """A folder for the job in the shared folder of the project's VM (started if needed)."""
        lease = await self.lease(project)
        lease.last_used = time.monotonic()
        folder = lease.shared / job_id
        folder.mkdir(parents=True, exist_ok=True)
        return folder

    async def run(self, project: str, folder: Path) -> JobResult:
        """Run the job in the project's VM, one job at a time per VM."""
        lease = await self.lease(project)
        async with lease.lock:
            lease.last_used = time.monotonic()
            code, out = await self.tart("exec", lease.name, self.settings.command,
                                        f"{MOUNT}/{folder.name}", timeout=None)  # fmt: skip
            lease.last_used = time.monotonic()
            return read_result(folder, code, out)

    async def lease(self, project: str) -> Lease:
        """The project's running VM; a new one when it has none."""
        async with self.starting.setdefault(project, asyncio.Lock()):
            found = self.leases.get(project)
            if found is not None and found.process.returncode is None:
                return found
            await self.vms.acquire()
            try:
                lease = await self.start_vm(project)
            except BaseException:
                self.vms.release()
                raise
            self.leases[project] = lease
            return lease

    async def start_vm(self, project: str) -> Lease:
        """Clone the image, start it with the project's shared folder, wait until it answers."""
        name = f"forge-{project[:16]}-{int(time.time())}"
        shared = self.settings.work / name
        shared.mkdir(parents=True, exist_ok=True)
        code, out = await self.tart("clone", self.settings.image, name)
        if code != 0:
            raise RuntimeError(f"tart clone failed: {out.strip()[-300:]}")
        argv = [self.settings.tart, "run", name, "--no-graphics", f"--dir=jobs:{shared}"]
        if self.settings.network == "softnet":
            argv.append("--net-softnet")
        process = await asyncio.create_subprocess_exec(
            *argv, stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )  # fmt: skip
        lease = Lease(name, shared, process)
        if not await self.wait_until_ready(lease):
            await self.end(lease)
            raise RuntimeError(f"the VM {name} did not start in time")
        return lease

    async def wait_until_ready(self, lease: Lease) -> bool:
        """True once the VM runs commands (Tart's guest agent answers)."""
        deadline = time.monotonic() + self.settings.boot_timeout_s
        while time.monotonic() < deadline and lease.process.returncode is None:
            code, _ = await self.tart("exec", lease.name, "true", timeout=30)
            if code == 0:
                return True
            await asyncio.sleep(2)
        return False

    async def reap(self) -> None:
        """Delete the VMs of projects that had no job for a while."""
        now = time.monotonic()
        for project, lease in list(self.leases.items()):
            idle = now - lease.last_used > self.settings.idle_s
            if (idle and not lease.lock.locked()) or lease.process.returncode is not None:
                del self.leases[project]
                await self.end(lease)
                self.vms.release()

    async def drop(self, project: str) -> None:
        """Delete a project's VM now (a one-off VM, e.g. for signing an export)."""
        lease = self.leases.pop(project, None)
        if lease is not None:
            await self.end(lease)
            self.vms.release()

    async def end(self, lease: Lease) -> None:
        """Stop and delete a VM and its shared folder."""
        await self.tart("stop", lease.name, timeout=120)
        with contextlib.suppress(ProcessLookupError):
            lease.process.kill()
        await lease.process.wait()
        await self.tart("delete", lease.name, timeout=120)
        await asyncio.to_thread(remove_tree, lease.shared)

    async def close(self) -> None:
        """Delete every VM."""
        for project in list(self.leases):
            lease = self.leases.pop(project)
            await self.end(lease)
            self.vms.release()

    async def tart(self, *args: str, timeout: float | None = 120) -> tuple[int, str]:
        """Run tart; its exit code and output."""
        proc = await asyncio.create_subprocess_exec(
            self.settings.tart, *args, stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
        )  # fmt: skip
        try:
            out, _ = await asyncio.wait_for(proc.communicate(), timeout)
        except TimeoutError:
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
            await proc.wait()
            return -1, f"tart {args[0]} took longer than {timeout:.0f} s"
        code = proc.returncode if proc.returncode is not None else -1
        return code, out.decode("utf-8", "replace")


def read_result(folder: Path, code: int, out: str) -> JobResult:
    """The result the job wrote; or why it wrote none."""
    try:
        return JobResult.model_validate_json((folder / RESULT).read_text("utf-8"))
    except (OSError, ValueError):
        tail = out.strip().splitlines()[-5:]
        log.warning("job in %s wrote no result (exit %s): %s", folder, code, json.dumps(tail))
        error = f"the job in the VM ended without a result (exit {code})"
        return JobResult(ok=False, error=error, hint="see the Mac worker's log")


def remove_tree(path: Path) -> None:
    """Delete a folder and everything in it."""
    shutil.rmtree(path, ignore_errors=True)
