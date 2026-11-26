"""A Windows desktop for Forge's agent (W62): the app published and started in a Windows Sandbox
that sees only the session's folder and the tools (read only), requests and answers through the
control folder, the machine's one sandbox held for the session, and links refused."""

import io
import json
import sys
import tarfile
from pathlib import Path

import pytest

from forge_winworker.device_script import SCRIPT
from forge_winworker.device_wire import DeviceAction, DeviceOffer
from forge_winworker.sandbox import SandboxRunner
from forge_winworker.windows_device import DeviceProblem, WindowsDevice, device_config

OFFER = DeviceOffer(
    id="a" * 32, platform="windows", project="b" * 32, start_timeout_s=30, max_s=600
)
PNG = b"\x89PNG\r\n\x1a\nscreen"

# Plays the sandbox: finds the session's folder in the .wsb, starts "the app" and answers each
# request as device.ps1 would (or fails the start, or leaves a link, as asked in device.json).
STANDIN = r"""
import json, os, re, sys, time
from pathlib import Path
wsb = Path(sys.argv[1]).read_text()
folder = Path(re.search(r"<HostFolder>(.*?)</HostFolder>", wsb).group(1))
control = folder / ".device"
job = json.loads((control / "device.json").read_text())
mode = (folder / "src" / "mode").read_text().strip() if (folder / "src" / "mode").exists() else ""
def answer(seq, **fields):
    (control / f"screen-{seq}.png").write_bytes(b"\x89PNG\r\n\x1a\nscreen")
    (control / f"res-{seq}.tmp").write_text(json.dumps({"seq": seq, **fields}), "utf-8-sig")
    os.replace(control / f"res-{seq}.tmp", control / f"res-{seq}.json")
if mode == "broken":
    answer(0, ok=False, error="the Windows app did not build", text="App.cs(3): error CS1002")
    sys.exit(0)
answer(0, ok=True, text=f"{job['exe']} runs", width=1280, height=800)
while not (control / "stop").exists():
    for req in sorted(control.glob("req-*.json")):
        action = json.loads(req.read_text())
        req.unlink()
        seq = action["seq"]
        if mode == "link":
            os.symlink(folder / "nowhere", control / f"res-{seq}.json")  # dangles, as on macOS
            continue
        text = f"did {action['kind']} {action.get('target', '')}"
        if action["kind"] == "read":
            text = "Button 'Sign in' (40,60,120,32)"
        answer(seq, ok=True, text=text, width=1280, height=800)
    time.sleep(0.05)
"""


def packed(tmp_path: Path, mode: str = "", app: bool = True) -> Path:
    archive = tmp_path / "source.tar.gz"
    files = {"README.md": b"lehm", "mode": mode.encode()}
    if app:
        files["windows/App/App.csproj"] = (
            b"<Project><PropertyGroup><AssemblyName>Lehm</AssemblyName></PropertyGroup></Project>"
        )
    with tarfile.open(archive, "w:gz") as tar:
        for name, data in files.items():
            info = tarfile.TarInfo(f"./{name}")
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return archive


@pytest.fixture
def sandbox(tmp_path: Path) -> SandboxRunner:
    script = tmp_path / "standin.py"
    script.write_text(STANDIN, encoding="utf-8")
    return SandboxRunner(tmp_path / "work", launcher=(sys.executable, str(script)), stops=(),
                         tools=[tmp_path / "dotnet"], memory_mb=4096)  # fmt: skip


def test_the_sandbox_sees_the_session_and_the_tools_read_only(tmp_path: Path) -> None:
    config = device_config(tmp_path / "s", [tmp_path / "dotnet"], 4096)
    assert (
        f"<HostFolder>{tmp_path / 's'}</HostFolder>"
        "<SandboxFolder>C:\\job</SandboxFolder><ReadOnly>false" in config
    )
    assert "<SandboxFolder>C:\\forge-tools\\0</SandboxFolder><ReadOnly>true" in config
    assert "<ClipboardRedirection>Disable" in config and "<ProtectedClient>Enable" in config
    assert "device.ps1" in config and "<MemoryInMB>4096" in config
    for part in ("SendKeys", "UIAutomationClient", "CopyFromScreen", "ProcessIdProperty", "stop"):
        assert part in SCRIPT, part  # the app's own windows only, and it stops when asked


async def test_the_agent_works_in_the_app_and_builds_wait(
    tmp_path: Path, sandbox: SandboxRunner
) -> None:
    device = WindowsDevice(sandbox, OFFER)
    first = await device.start(packed(tmp_path))
    assert first.ok and first.text == "Lehm.exe runs" and first.image_b64 and first.width == 1280
    request = json.loads((device.control / "device.json").read_text())
    assert request == {"project": "windows\\App\\App.csproj", "exe": "Lehm.exe"}
    assert sandbox.lock.locked()  # the machine's one sandbox is the session's: builds wait
    tapped = await device.act(DeviceAction(seq=1, kind="tap", target="Sign in"))
    assert tapped.ok and tapped.text == "did tap Sign in" and tapped.image_b64
    read = await device.act(DeviceAction(seq=2, kind="read"))
    assert "Button 'Sign in'" in read.text
    await device.close()
    assert not sandbox.lock.locked() and not device.folder.exists()


async def test_a_failed_start_or_a_link_ends_the_session(
    tmp_path: Path, sandbox: SandboxRunner
) -> None:
    broken = WindowsDevice(sandbox, OFFER)
    with pytest.raises(DeviceProblem, match="did not build") as failed:
        await broken.start(packed(tmp_path, "broken"))
    assert "CS1002" in failed.value.hint
    await broken.close()
    missing = WindowsDevice(sandbox, OFFER)
    with pytest.raises(DeviceProblem, match="no Windows app"):
        await missing.start(packed(tmp_path, app=False))
    await missing.close()
    linked = WindowsDevice(sandbox, OFFER)  # a link is refused at once, even where it dangles
    await linked.start(packed(tmp_path, "link"))
    with pytest.raises(DeviceProblem, match="other than an answer"):
        await linked.act(DeviceAction(seq=1, kind="screenshot"))
    await linked.close()
    assert not sandbox.lock.locked()


def test_devices_run_only_in_a_sandbox_and_never_on_a_signing_machine(tmp_path: Path) -> None:
    from forge_winworker.client import WorkerClient
    from forge_winworker.runners import DirectRunner

    sandbox = SandboxRunner(tmp_path, tools=[])
    assert WorkerClient("https://forge.example", "t", sandbox, devices=True).devices
    assert not WorkerClient(
        "https://forge.example", "t", DirectRunner(tmp_path), devices=True
    ).devices
    signer = WorkerClient(
        "https://forge.example", "t", sandbox, devices=True, sign_template="sign {file}"
    )
    assert not signer.devices
