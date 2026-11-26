"""A Windows desktop for Forge's agent (W62): the product's Windows app, published and started in
a fresh Windows Sandbox, and driven by the script inside it (`device_script.py`).

The sandbox sees what a build's does: the session's own folder (to read and write) and, read
only, the folders of dotnet; networking stays on (NuGet, and the app's staging server). It holds
the machine's one sandbox for the whole session, so builds wait meanwhile. Requests and answers
go through the session's control folder; what the sandbox leaves there is read with care: a link
in place of an answer (which could point at this machine's files) ends the session.
"""

import asyncio
import base64
import contextlib
import json
import os
import shutil
import stat
import time
from pathlib import Path
from xml.sax.saxutils import escape

from forge_winworker.build import SourceError, assembly_name, unpack
from forge_winworker.device_script import SCRIPT
from forge_winworker.device_wire import MAX_TEXT, DeviceAction, DeviceAnswer, DeviceOffer
from forge_winworker.runners import job_environment
from forge_winworker.sandbox import JOB, TOOLS, SandboxRunner, reaped

APP = Path("windows/App/App.csproj")
CONTROL = ".device"
ANSWER_S = 60.0  # one action inside the sandbox
POLL_S = 0.2
MAX_SCREEN = 6 * 1024 * 1024  # bytes of one screenshot


class DeviceProblem(Exception):
    """The device could not do what was asked; `hint` says what may help."""

    def __init__(self, message: str, hint: str = "") -> None:
        super().__init__(message)
        self.hint = hint


def device_config(folder: Path, tools: list[Path], memory_mb: int) -> str:
    """The `.wsb` of a device session: its folder, the tools read only, a desktop to use."""
    shares = [(folder, JOB, False)] + [(t, f"{TOOLS}\\{n}", True) for n, t in enumerate(tools)]
    mapped = "".join(
        f"<MappedFolder><HostFolder>{escape(str(host))}</HostFolder>"
        f"<SandboxFolder>{escape(there)}</SandboxFolder>"
        f"<ReadOnly>{'true' if read_only else 'false'}</ReadOnly></MappedFolder>"
        for host, there, read_only in shares
    )
    command = (
        f"powershell.exe -NoProfile -ExecutionPolicy Bypass -File {JOB}\\{CONTROL}\\device.ps1"
    )
    return (
        "<Configuration><Networking>Enable</Networking><vGPU>Disable</vGPU>"
        "<AudioInput>Disable</AudioInput><VideoInput>Disable</VideoInput>"
        "<PrinterRedirection>Disable</PrinterRedirection>"
        "<ClipboardRedirection>Disable</ClipboardRedirection>"
        f"<ProtectedClient>Enable</ProtectedClient><MemoryInMB>{memory_mb}</MemoryInMB>"
        f"<MappedFolders>{mapped}</MappedFolders>"
        f"<LogonCommand><Command>{escape(command)}</Command></LogonCommand></Configuration>"
    )


def plain_file(path: Path, limit: int) -> bytes | None:
    """The file's bytes if it is a plain file (no link or junction on the way), else None."""
    for part in (path.parent, path):
        if part.is_symlink() or part.is_junction():
            return None
    try:
        handle = os.open(path, os.O_RDONLY | getattr(os, "O_BINARY", 0))
    except OSError:
        return None
    with os.fdopen(handle, "rb") as source:
        if not stat.S_ISREG(os.fstat(source.fileno()).st_mode):
            return None
        return source.read(limit + 1)


def pixels(value: object) -> int:
    """A screen size from the sandbox's answer, within reason."""
    return min(max(value, 0), 20_000) if isinstance(value, int) else 0


class WindowsDevice:
    """One device session in a Windows Sandbox on this machine."""

    def __init__(self, sandbox: SandboxRunner, offer: DeviceOffer) -> None:
        self.sandbox = sandbox
        self.offer = offer
        self.folder = sandbox.work / "devices" / offer.id
        self.control = self.folder / CONTROL
        self.launched: asyncio.subprocess.Process | None = None
        self.holding = False

    async def start(self, source: Path) -> DeviceAnswer:
        """Unpack the app, open the sandbox, and wait for the app's first screen."""
        await asyncio.to_thread(shutil.rmtree, self.folder, True)
        try:
            await asyncio.to_thread(unpack, source, self.folder / "src")
        except SourceError as error:
            raise DeviceProblem(f"the source was refused: {error}") from error
        project = self.folder / "src" / APP
        if not project.is_file():
            raise DeviceProblem("the product has no Windows app (windows/App)")
        self.control.mkdir(parents=True)
        exe = f"{assembly_name(project)}.exe"
        (self.control / "device.json").write_text(
            json.dumps({"project": str(APP).replace("/", "\\"), "exe": exe}), encoding="utf-8"
        )
        (self.control / "device.ps1").write_text(SCRIPT, encoding="utf-8-sig")
        wsb = self.control / "device.wsb"
        config = device_config(self.folder, self.sandbox.tools, self.sandbox.memory_mb)
        wsb.write_text(config, encoding="utf-8")
        await self.sandbox.lock.acquire()  # the machine's one sandbox, for the whole session
        self.holding = True
        try:
            self.launched = await asyncio.create_subprocess_exec(
                *self.sandbox.launcher, str(wsb), env=job_environment()
            )
        except OSError as err:
            raise DeviceProblem(f"Windows Sandbox cannot be started here: {err}") from err
        first = await self.answer(0, self.offer.start_timeout_s)
        if not first.ok:
            raise DeviceProblem(first.error or "the app did not start", first.text[-2000:])
        return first

    async def act(self, action: DeviceAction) -> DeviceAnswer:
        """Hand one action to the script in the sandbox; its answer and the screen."""
        request = action.model_dump(exclude_none=True)
        temporary = self.control / f"req-{action.seq}.tmp"
        temporary.write_text(json.dumps(request), encoding="utf-8")
        temporary.replace(self.control / f"req-{action.seq}.json")
        return await self.answer(action.seq, ANSWER_S)

    async def answer(self, seq: int, seconds: float) -> DeviceAnswer:
        """Wait for the script's answer to `seq`."""
        path = self.control / f"res-{seq}.json"
        deadline = time.monotonic() + seconds
        while not path.exists():
            launched = self.launched
            if launched is not None and launched.returncode not in (None, 0):
                raise DeviceProblem("Windows Sandbox stopped (is the Windows Sandbox feature on?)")
            if time.monotonic() > deadline:
                raise DeviceProblem("the app in the sandbox did not answer in time")
            await asyncio.sleep(POLL_S)
        return await asyncio.to_thread(self.read_answer, seq)

    def read_answer(self, seq: int) -> DeviceAnswer:
        """The script's answer to `seq`, with its screenshot."""
        raw = plain_file(self.control / f"res-{seq}.json", MAX_TEXT * 2)
        if raw is None:
            raise DeviceProblem("the sandbox left something other than an answer")
        try:
            found = json.loads(raw.decode("utf-8-sig", "replace"))
        except ValueError as err:
            raise DeviceProblem("the sandbox's answer is not JSON") from err
        image = plain_file(self.control / f"screen-{seq}.png", MAX_SCREEN) or b""
        for name in (f"res-{seq}.json", f"screen-{seq}.png"):
            with contextlib.suppress(OSError):
                (self.control / name).unlink()
        return DeviceAnswer(
            seq=seq,
            ok=bool(found.get("ok")),
            text=str(found.get("text") or "")[:MAX_TEXT],
            error=str(found.get("error") or "")[:2000],
            image_b64=base64.b64encode(image).decode() if len(image) <= MAX_SCREEN else "",
            media_type="image/png",
            width=pixels(found.get("width")),
            height=pixels(found.get("height")),
        )

    async def close(self) -> None:
        """Ask the script to stop, close the sandbox and forget the session's files."""
        with contextlib.suppress(OSError):
            (self.control / "stop").write_text("", encoding="utf-8")
        if self.launched is not None:
            await asyncio.sleep(1)
            await self.sandbox.stop()
            await reaped(self.launched)
        if self.holding:
            self.sandbox.lock.release()
            self.holding = False
        await asyncio.to_thread(shutil.rmtree, self.folder, True)
