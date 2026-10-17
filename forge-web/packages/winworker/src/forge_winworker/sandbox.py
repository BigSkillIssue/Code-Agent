"""A throwaway Windows Sandbox for each command of a build job (W41).

Windows Sandbox starts a fresh copy of Windows that is gone once it closes. Each command a build
job runs (the app's tests, its publish, Velopack's pack) gets one of its own: it sees the job's
folder (to read and write) and, read only, the folders of dotnet and vpk on this machine, and
nothing else of it: not the worker's token, not other jobs, not this user's files. The command's
output and exit code come back through the job's folder, and the sandbox shuts itself down; one
that does not start, or that the job's time runs out on, is stopped.

What the sandbox leaves in the job's folder is the product's own doing, so it is read with care:
a command that made links there (which could point at this machine's files) fails. Networking
stays on, because NuGet fetches the packages; they go into the job's own folder, so no job can
change another's. Windows Sandbox runs one at a time; it needs Windows 10 or 11 Pro or Enterprise
with the "Windows Sandbox" feature, and nested virtualisation on a VM.
"""

import asyncio
import contextlib
import json
import os
import secrets
import shutil
import time
from pathlib import Path
from xml.sax.saxutils import escape

from forge_winworker.runners import MAX_OUTPUT, QUIET, job_environment

JOB = "C:\\job"  # the job's folder inside the sandbox
TOOLS = "C:\\forge-tools"  # dotnet's and vpk's folders inside it, read only
CONTROL = ".sandbox"  # in the job's folder: each command's script, request and result
START_S = 300.0  # time for a sandbox to start
POLL_S = 0.5
LAUNCHER = ("WindowsSandbox.exe",)
SANDBOX_PROCESSES = ("WindowsSandboxRemoteSession.exe", "WindowsSandboxClient.exe",
                     "WindowsSandbox.exe")  # fmt: skip
STOPS = tuple(("taskkill", "/F", "/IM", name) for name in SANDBOX_PROCESSES)
SCRIPT = r"""$ErrorActionPreference = 'Continue'
$control = 'C:\job\.sandbox'
$nonce = '{nonce}'
New-Item -ItemType File -Force -Path "$control\$nonce.started" | Out-Null
$code = 1
try {{
  $job = Get-Content -Raw -Encoding UTF8 -Path "$control\$nonce.json" | ConvertFrom-Json
  foreach ($pair in $job.env.PSObject.Properties) {{
    Set-Item -Path "env:$($pair.Name)" -Value $pair.Value
  }}
  $env:PATH = (@($job.path) -join ';') + ';' + $env:PATH
  Set-Location -LiteralPath $job.cwd
  $argv = @($job.argv)
  $rest = @()
  if ($argv.Length -gt 1) {{ $rest = $argv[1..($argv.Length - 1)] }}
  & $argv[0] @rest 2>&1 | Out-File -Encoding utf8 -FilePath "$control\$nonce.out"
  $code = $LASTEXITCODE
}} catch {{
  $_ | Out-File -Append -Encoding utf8 -FilePath "$control\$nonce.out"
}}
Set-Content -Encoding Ascii -Path "$control\$nonce.exit" -Value "$code"
shutdown.exe /s /t 0
"""


def tool_folders() -> list[Path]:
    """The folders of dotnet and vpk on this machine (which the sandbox gets, read only)."""
    found = [shutil.which(tool) for tool in ("dotnet", "vpk")]
    return list(dict.fromkeys(Path(path).resolve().parent for path in found if path))


def inside(value: str, job: Path) -> str:
    """A path in the job's folder as the sandbox sees it; anything else as it is."""
    root = str(job)
    if value != root and not value.startswith(root + os.sep):
        return value
    rest = value[len(root):].lstrip("\\/")  # fmt: skip
    return JOB + ("\\" + rest.replace("/", "\\") if rest else "")


def config(job: Path, tools: list[Path], nonce: str, memory_mb: int) -> str:
    """The sandbox's `.wsb`: the job's folder, the tools read only, nothing else shared."""
    folders = [(job, JOB, False)] + [(t, f"{TOOLS}\\{n}", True) for n, t in enumerate(tools)]
    mapped = "".join(
        f"<MappedFolder><HostFolder>{escape(str(host))}</HostFolder>"
        f"<SandboxFolder>{escape(there)}</SandboxFolder>"
        f"<ReadOnly>{'true' if read_only else 'false'}</ReadOnly></MappedFolder>"
        for host, there, read_only in folders
    )
    command = (f"powershell.exe -NoProfile -ExecutionPolicy Bypass -File "
               f"{JOB}\\{CONTROL}\\{nonce}.ps1")  # fmt: skip
    return (
        "<Configuration><Networking>Enable</Networking><vGPU>Disable</vGPU>"
        "<AudioInput>Disable</AudioInput><VideoInput>Disable</VideoInput>"
        "<PrinterRedirection>Disable</PrinterRedirection>"
        "<ClipboardRedirection>Disable</ClipboardRedirection>"
        f"<ProtectedClient>Enable</ProtectedClient><MemoryInMB>{memory_mb}</MemoryInMB>"
        f"<MappedFolders>{mapped}</MappedFolders>"
        f"<LogonCommand><Command>{escape(command)}</Command></LogonCommand></Configuration>"
    )


def request(command: list[str], cwd: Path, job: Path, tools: list[Path]) -> str:
    """What the sandbox's script runs: the command, its folder and its environment."""
    env = QUIET | {"NUGET_PACKAGES": f"{JOB}\\.nuget", "DOTNET_ROOT": f"{TOOLS}\\0"}
    return json.dumps({"argv": [inside(part, job) for part in command],
                       "cwd": inside(str(cwd), job), "env": env,
                       "path": [f"{TOOLS}\\{n}" for n in range(len(tools))]})  # fmt: skip


def links_in(folder: Path) -> list[Path]:
    """Links (symbolic links, junctions) under `folder`, which a sandbox must not leave."""
    found = []
    for root, dirs, files in os.walk(folder):  # never into a link: it is not followed
        for name in [*dirs, *files]:
            path = Path(root) / name
            if path.is_symlink() or path.is_junction():
                found.append(path)
    return found


class SandboxRunner:
    """Runs each command in a Windows Sandbox of its own, one sandbox at a time; the jobs'
    folders are under `work`, as the direct runner keeps them."""

    def __init__(self, work: Path, launcher: tuple[str, ...] = LAUNCHER,
                 stops: tuple[tuple[str, ...], ...] = STOPS, tools: list[Path] | None = None,
                 memory_mb: int = 8192, start_s: float = START_S) -> None:  # fmt: skip
        self.work, self.launcher, self.stops = work, launcher, stops
        self.tools = tool_folders() if tools is None else tools
        self.memory_mb, self.start_s = memory_mb, start_s
        self.lock = asyncio.Lock()

    async def prepare(self, job_id: str) -> Path:
        folder = self.work / "jobs" / job_id
        await asyncio.to_thread(shutil.rmtree, folder, True)
        folder.mkdir(parents=True)
        return folder

    async def remove(self, folder: Path) -> None:
        await asyncio.to_thread(shutil.rmtree, folder, True)

    def job_of(self, cwd: Path) -> Path:
        """The job's folder that `cwd` is in."""
        jobs = self.work / "jobs"
        return jobs / cwd.resolve().relative_to(jobs.resolve()).parts[0]

    async def run(self, command: list[str], cwd: Path) -> tuple[int, str]:
        job = self.job_of(cwd)
        control, nonce = job / CONTROL, secrets.token_hex(8)
        control.mkdir(exist_ok=True)
        (control / f"{nonce}.json").write_text(request(command, cwd, job, self.tools), "utf-8")
        (control / f"{nonce}.ps1").write_text(SCRIPT.format(nonce=nonce), encoding="utf-8-sig")
        wsb = control / f"{nonce}.wsb"
        wsb.write_text(config(job, self.tools, nonce, self.memory_mb), encoding="utf-8")
        async with self.lock:
            try:
                launched = await asyncio.create_subprocess_exec(*self.launcher, str(wsb),
                                                                env=job_environment())  # fmt: skip
            except OSError as err:
                return 1, f"Windows Sandbox cannot be started here: {err}"
            try:
                code, output = await self.result(control, nonce, launched)
            finally:
                await self.stop()
                await reaped(launched)
        links = await asyncio.to_thread(links_in, job)
        if links:
            names = ", ".join(str(p.relative_to(job)) for p in links[:5])
            return 1, f"the command made links in the job's folder, which are refused: {names}"
        return code, output

    async def result(self, control: Path, nonce: str,
                     launched: asyncio.subprocess.Process) -> tuple[int, str]:  # fmt: skip
        """Wait for the command's exit code; its output."""
        deadline = time.monotonic() + self.start_s
        while not (control / f"{nonce}.exit").is_file():
            if not (control / f"{nonce}.started").is_file():
                failed = launched.returncode not in (None, 0)
                if failed or time.monotonic() > deadline:
                    return 1, "Windows Sandbox did not start (is the Windows Sandbox feature on?)"
            await asyncio.sleep(POLL_S)
        await asyncio.sleep(POLL_S)  # the output is closed before the exit code is written
        exit_text = (control / f"{nonce}.exit").read_text("ascii", "replace").strip()
        out = control / f"{nonce}.out"
        output = out.read_bytes().decode("utf-8-sig", "replace") if out.is_file() else ""
        return int(exit_text) if exit_text.lstrip("-").isdigit() else 1, output[-MAX_OUTPUT:]

    async def stop(self) -> None:
        """Close whatever is left of the sandbox."""
        for stop in self.stops:
            with contextlib.suppress(OSError):
                process = await asyncio.create_subprocess_exec(
                    *stop, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL
                )
                await process.wait()


async def reaped(process: asyncio.subprocess.Process) -> None:
    """Wait for the launcher to end once its sandbox is closed (and end it if it does not)."""
    try:
        await asyncio.wait_for(process.wait(), 10)
    except TimeoutError:
        with contextlib.suppress(ProcessLookupError):
            process.kill()
        await process.wait()
