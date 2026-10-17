"""A throwaway Windows Sandbox for each command of a Windows build (W41). The sandbox is a
stand-in: it reads the `.wsb` the worker wrote, takes the command from the job's folder, runs it
with stand-ins for dotnet and vpk (seeing the job's folder and the tools only, by their paths in
the sandbox) and writes the result back, as the PowerShell script in the real sandbox does. The
sandbox sees only the job's folder and, read only, the tools; the worker's token never gets in;
one that does not start, or runs out of time, is stopped; links it leaves are refused."""

import json
import os
import sys
import textwrap
from pathlib import Path
from typing import Any
from xml.etree import ElementTree

import httpx
import pytest

from forge_winworker.cli import mode_problem
from forge_winworker.sandbox import SandboxRunner, config, inside, links_in
from support import LiveServer
from test_hosting_deploy import short_polls, until
from test_winworker import (
    TRX,
    client,
    finished,
    program,
    server,
    short_worker_polls,
    started,
    windows_project,
    worker_online,
    worker_token,
)

__all__ = ["client", "server", "short_polls", "short_worker_polls"]

SANDBOX = r"""
import json, os, pathlib, subprocess, sys, time
from xml.etree import ElementTree

wsb = pathlib.Path(sys.argv[1])
root = ElementTree.fromstring(wsb.read_text("utf-8"))
mapped = {m.findtext("SandboxFolder"): (m.findtext("HostFolder"), m.findtext("ReadOnly"))
          for m in root.iter("MappedFolder")}
job = pathlib.Path(mapped["C:\\job"][0])
nonce = root.findtext("LogonCommand/Command").rsplit("\\", 1)[1].removesuffix(".ps1")
control = job / ".sandbox"
record = pathlib.Path(os.environ["SANDBOX_RECORD"])
with record.open("a", encoding="utf-8") as seen:
    seen.write(json.dumps({"wsb": wsb.read_text("utf-8"), "env": sorted(os.environ),
                           "request": json.loads((control / f"{nonce}.json").read_text())}) + "\n")
mode = os.environ.get("SANDBOX_MODE", "run")
if mode == "never":
    sys.exit(0)
(control / f"{nonce}.started").touch()
pathlib.Path(os.environ["SANDBOX_PID"]).write_text(str(os.getpid()))
if mode == "hang":
    time.sleep(600)

def host(value):
    for there, (here, _) in mapped.items():
        if value == there or value.startswith(there + "\\"):
            return here + value[len(there):].replace("\\", "/")
    return value

request = json.loads((control / f"{nonce}.json").read_text())
tools = [host(p) for p in request["path"]]
exe = next(str(pathlib.Path(t) / request["argv"][0]) for t in tools
           if (pathlib.Path(t) / request["argv"][0]).is_file())
if mode == "link":
    (job / "publish").mkdir(exist_ok=True)
    (job / "publish" / "secrets").symlink_to(pathlib.Path.home())
done = subprocess.run([sys.executable, exe, *[host(a) for a in request["argv"][1:]]],
                      cwd=host(request["cwd"]), capture_output=True, text=True,
                      env={**request["env"], "PATH": os.environ.get("PATH", "")})
(control / f"{nonce}.out").write_text(done.stdout + done.stderr, encoding="utf-8")
(control / f"{nonce}.exit").write_text(str(done.returncode), encoding="ascii")
"""

DOTNET = """
import pathlib, sys
args = sys.argv[1:]
if args[0] == "test":
    results = pathlib.Path(args[args.index("--results-directory") + 1])
    results.mkdir(parents=True, exist_ok=True)
    (results / "run.trx").write_text({trx!r})
    print("Passed!")
elif args[0] == "publish":
    out = pathlib.Path(args[args.index("-o") + 1])
    out.mkdir(parents=True)
    (out / "Shop.exe").write_bytes({program!r})
    (out / "Shop.dll").write_bytes(b"the app")
    print("Build succeeded.")
else:
    sys.exit(2)
"""

VPK = """
import hashlib, json, pathlib, sys
args = sys.argv[1:]
value = {{args[i]: args[i + 1] for i in range(1, len(args) - 1, 2)}}
out = pathlib.Path(value["--outputDir"])
pack_id, version = value["--packId"], value["--packVersion"]
out.mkdir(parents=True, exist_ok=True)
(out / f"{{pack_id}}-win-Setup.exe").write_bytes({program!r} + b"setup")
(out / f"{{pack_id}}-win-Portable.zip").write_bytes(b"PK\\x05\\x06" + b"\\0" * 18)
package = out / f"{{pack_id}}-{{version}}-full.nupkg"
package.write_bytes(b"PK the full package " + version.encode())
asset = {{"PackageId": pack_id, "Version": version, "Type": "Full", "FileName": package.name,
         "Size": package.stat().st_size,
         "SHA256": hashlib.sha256(package.read_bytes()).hexdigest().upper()}}
(out / "releases.win.json").write_text(json.dumps({{"Assets": [asset]}}))
print("vpk packed", pack_id)
"""


@pytest.fixture
def machine(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    """A stand-in sandbox, dotnet's and vpk's folders, and where the sandbox notes what it saw."""
    places = {name: tmp_path / name for name in ("dotnet", "tools", "work")}
    for folder in places.values():
        folder.mkdir()
    (places["dotnet"] / "dotnet").write_text(
        DOTNET.format(trx=TRX.format(n=3, passed=3, failed=0), program=program(False))
    )
    (places["tools"] / "vpk").write_text(VPK.format(program=program(False)))
    places["launcher"] = tmp_path / "sandbox.py"
    places["launcher"].write_text(textwrap.dedent(SANDBOX))
    places["record"], places["pid"] = tmp_path / "seen.jsonl", tmp_path / "pid"
    monkeypatch.setenv("SANDBOX_RECORD", str(places["record"]))
    monkeypatch.setenv("SANDBOX_PID", str(places["pid"]))
    monkeypatch.setenv("WIN_WORKER_TOKEN", "fww_never_in_a_sandbox")
    return places


def runner(machine: dict[str, Path], start_s: float = 30) -> SandboxRunner:
    kill = (
        "import os, pathlib, signal, sys\np = pathlib.Path(sys.argv[1])\n"
        "if p.exists():\n    os.kill(int(p.read_text()), signal.SIGTERM)\n    p.unlink()"
    )
    return SandboxRunner(machine["work"], launcher=(sys.executable, str(machine["launcher"])),
                         stops=((sys.executable, "-c", kill, str(machine["pid"])),),
                         tools=[machine["dotnet"], machine["tools"]], start_s=start_s)  # fmt: skip


def seen(machine: dict[str, Path]) -> list[dict[str, Any]]:
    lines = machine["record"].read_text("utf-8").splitlines()
    return [json.loads(line) for line in lines]


async def test_the_sandbox_sees_only_the_job_and_the_tools(machine: dict[str, Path]) -> None:
    sandbox = runner(machine)
    job = await sandbox.prepare("a" * 32)
    (job / "src").mkdir()
    code, output = await sandbox.run(["dotnet", "publish", "App.csproj", "-o", str(job / "out")],
                                     job / "src")  # fmt: skip
    assert (code, output.strip()) == (0, "Build succeeded."), output
    assert (job / "out" / "Shop.exe").is_file()  # written through the job's folder
    [first] = seen(machine)
    wsb = ElementTree.fromstring(first["wsb"])
    shared = {m.findtext("HostFolder"): (m.findtext("SandboxFolder"), m.findtext("ReadOnly"))
              for m in wsb.iter("MappedFolder")}  # fmt: skip
    assert shared == {str(job): ("C:\\job", "false"),
                      str(machine["dotnet"]): ("C:\\forge-tools\\0", "true"),
                      str(machine["tools"]): ("C:\\forge-tools\\1", "true")}  # fmt: skip
    for off in ("vGPU", "AudioInput", "VideoInput", "PrinterRedirection", "ClipboardRedirection"):
        assert wsb.findtext(off) == "Disable"
    request = first["request"]
    assert request["argv"] == ["dotnet", "publish", "App.csproj", "-o", "C:\\job\\out"]
    assert (
        request["cwd"] == "C:\\job\\src" and request["env"]["NUGET_PACKAGES"] == "C:\\job\\.nuget"
    )
    assert "WIN_WORKER_TOKEN" not in request["env"] and "WIN_WORKER_TOKEN" not in first["env"]
    assert "fww_never_in_a_sandbox" not in json.dumps(first)
    await sandbox.remove(job)


async def test_a_sandbox_that_never_starts_or_never_ends_is_stopped(
    machine: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    import asyncio

    sandbox = runner(machine, start_s=1)
    job = await sandbox.prepare("b" * 32)
    monkeypatch.setenv("SANDBOX_MODE", "never")
    code, output = await sandbox.run(["dotnet", "test"], job)
    assert code == 1 and "did not start" in output
    monkeypatch.setenv("SANDBOX_MODE", "hang")
    with pytest.raises(TimeoutError):  # the job's time ran out
        await asyncio.wait_for(sandbox.run(["dotnet", "test"], job), 5)
    assert not machine["pid"].exists()  # the stop command closed the sandbox
    await sandbox.remove(job)


async def test_links_left_in_the_job_folder_fail_the_command(
    machine: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    sandbox = runner(machine)
    job = await sandbox.prepare("c" * 32)
    monkeypatch.setenv("SANDBOX_MODE", "link")
    code, output = await sandbox.run(["dotnet", "publish", "x", "-o", str(job / "out")], job)
    assert code == 1 and "links" in output and "secrets" in output
    assert links_in(job) == [job / "publish" / "secrets"]
    await sandbox.remove(job)


async def test_a_whole_build_runs_in_sandboxes(
    server: LiveServer, client: httpx.AsyncClient, machine: dict[str, Path]
) -> None:
    sandbox = runner(machine)
    async with worker_online(server.url, await worker_token(client), sandbox):
        project_id = await windows_project(server, client)
        build = await started(client, project_id)
        done = await until(lambda: finished(client, project_id, build["id"]))
        assert done["status"] == "built", done["error"]
        assert done["tests_passed"] == 3 and "Shop-1.0.1-full.nupkg" in done["files"]
    commands = [entry["request"]["argv"][:2] for entry in seen(machine)]
    assert commands == [["dotnet", "test"], ["dotnet", "publish"], ["vpk", "pack"]]
    assert not list((machine["work"] / "jobs").iterdir())


def test_paths_the_config_and_the_modes() -> None:
    job = Path(os.sep, "w", "jobs", "j1")
    assert inside(str(job / "src" / "a.txt"), job) == "C:\\job\\src\\a.txt"
    assert inside(str(job), job) == "C:\\job"
    assert inside(str(job) + "x", job) == str(job) + "x"  # a neighbour is not inside
    assert inside("-p:Version=1.0.1", job) == "-p:Version=1.0.1"
    made = config(Path("C:/w/jobs/j<1>"), [], "n1", 4096)
    assert "j&lt;1&gt;" in made and "<MemoryInMB>4096</MemoryInMB>" in made
    assert mode_problem(False, "", sandbox=True) == ""
    assert "one at a time" in mode_problem(False, "", sandbox=True, slots=2)
    assert "one of" in mode_problem(True, "", sandbox=True)
    assert "--sandbox" in mode_problem(False, "")
