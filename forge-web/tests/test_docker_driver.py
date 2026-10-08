"""Docker isolation: the container settings (offline) and, with a Docker daemon, the real thing."""

import asyncio
import os
import subprocess
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest

from forge_web.containers.docker import DockerDriver
from forge_web.sandbox_cli import build_command, source_root
from forge_web.sandbox_client import SandboxClient
from forge_web.settings import SandboxSettings
from support import LiveServer, call, dev_settings, fake_script

IMAGE = os.environ.get("FORGE_WEB_TEST_IMAGE", "forge-web-sandbox:dev")


def test_run_arguments_harden_the_container() -> None:
    driver = DockerDriver(SandboxSettings(image="img:1", cpus=1.5, memory="2g", pids=256))
    args = driver.run_args("abc123", "runsc")
    joined = " ".join(args)
    assert args[-1] == "img:1"
    for expected in (
        "--network none", "--read-only", "--cap-drop ALL", "--security-opt no-new-privileges",
        "--cpus 1.5", "--memory 2g", "--pids-limit 256", "--runtime runsc", "--init",
        "source=forge-web-abc123-workspace,target=/workspace",
        "source=forge-web-abc123-home,target=/home/forge",
    ):  # fmt: skip
        assert expected in joined, expected
    added = [args[i + 1] for i, a in enumerate(args) if a == "--cap-add"]
    assert set(added) == {"CHOWN", "DAC_OVERRIDE", "FOWNER", "SETUID", "SETGID", "KILL"}
    assert "--runtime" not in driver.run_args("abc123", None)
    assert "--privileged" not in joined and "docker.sock" not in joined


def test_sandbox_build_command_uses_the_checkout() -> None:
    root = source_root()
    assert root is not None and (root / "src" / "forge").is_dir()
    command = build_command("docker", root, "forge-web-sandbox:x", ["--pull"])
    assert command[:2] == ["docker", "build"] and command[-1] == str(root)
    assert "--pull" in command and "forge-web-sandbox:x" in command


# With a Docker daemon --------------------------------------------------------------------------


def docker_ready() -> bool:
    try:
        result = subprocess.run(
            ["docker", "image", "inspect", IMAGE], capture_output=True, timeout=30
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


needs_docker = [
    pytest.mark.docker,
    pytest.mark.skipif(not docker_ready(), reason=f"needs Docker and the image {IMAGE}"),
]


@pytest.fixture
async def driver() -> AsyncIterator[DockerDriver]:
    created = DockerDriver(SandboxSettings(image=IMAGE, cpus=1, memory="1g", pids=256))
    yield created
    for project_id in ("dtest-a", "dtest-b", "dtest-c"):
        await created.remove(project_id)


async def client_for(driver: DockerDriver, project_id: str) -> SandboxClient:
    client = SandboxClient(await driver.connect(project_id))
    await client.start()
    return client


async def run_as_agent(client: SandboxClient, code: str) -> list[str]:
    started = await client.call("procs.start", {"argv": ["python3", "-c", code]})
    for _ in range(200):
        output = await client.call("procs.output", {"id": started["id"]})
        if not output["running"]:
            return list(output["lines"])
        await asyncio.sleep(0.05)
    raise AssertionError("the program did not finish")


@needs_docker[0]
@needs_docker[1]
async def test_container_is_hardened_and_serves_the_workspace(driver: DockerDriver) -> None:
    client = await client_for(driver, "dtest-a")
    try:
        assert client.hello is not None and client.hello.info["workspace"] == "/workspace"
        await client.call(
            "fs.write", {"path": "src/x.py", "text": "print(1)\n", "create_dirs": True}
        )
        assert (await client.call("fs.read", {"path": "src/x.py"}))["text"] == "print(1)\n"
        whoami = await run_as_agent(client, "import os; print(os.getuid(), os.getgid())")
        assert whoami == ["1000 1000"]
        owner = await run_as_agent(
            client, "import os; print(os.stat('/workspace/src/x.py').st_uid)"
        )
        assert owner == ["1000"]
    finally:
        await client.close()
    info = await driver.inspect("dtest-a")
    assert info is not None
    host = info["HostConfig"]
    assert host["ReadonlyRootfs"] and host["NetworkMode"] == "none"
    assert host["CapDrop"] == ["ALL"] and "no-new-privileges" in host["SecurityOpt"]
    assert host["PidsLimit"] == 256 and host["Memory"] == 1024**3
    assert not host.get("Binds")  # volumes only, no host folders


@needs_docker[0]
@needs_docker[1]
async def test_the_agent_cannot_reach_the_daemon_or_the_network(driver: DockerDriver) -> None:
    client = await client_for(driver, "dtest-b")
    try:
        socket_try = (
            "import socket\n"
            "s = socket.socket(socket.AF_UNIX)\n"
            "try:\n s.connect('/run/forge-sandbox/daemon.sock'); print('connected')\n"
            "except OSError as e:\n print(type(e).__name__)"
        )
        assert (await run_as_agent(client, socket_try))[0] in (
            "PermissionError",
            "FileNotFoundError",
        )
        net_try = (
            "import socket\n"
            "for host in ('1.1.1.1', '169.254.169.254', '172.17.0.1'):\n"
            " try:\n  socket.create_connection((host, 80), timeout=2); print('reached', host)\n"
            " except OSError:\n  print('blocked')"
        )
        assert await run_as_agent(client, net_try) == ["blocked", "blocked", "blocked"]
    finally:
        await client.close()


@needs_docker[0]
@needs_docker[1]
async def test_idle_stop_and_restart_keep_the_files(driver: DockerDriver, tmp_path: Path) -> None:
    client = await client_for(driver, "dtest-c")
    await client.call("fs.write", {"path": "keep.txt", "text": "still here"})
    await client.close()
    await driver.stop("dtest-c")
    info = await driver.inspect("dtest-c")
    assert info is not None and not info["State"]["Running"]
    again = await client_for(driver, "dtest-c")
    try:
        assert (await again.call("fs.read", {"path": "keep.txt"}))["text"] == "still here"
    finally:
        await again.close()
    await driver.remove("dtest-c")
    assert await driver.inspect("dtest-c") is None
    volumes = subprocess.run(
        ["docker", "volume", "ls", "-q"], capture_output=True, text=True
    ).stdout
    assert "forge-web-dtest-c" not in volumes


@pytest.fixture
def docker_data(tmp_path: Path) -> Iterator[Path]:
    yield tmp_path / "data"
    names = subprocess.run(
        ["docker", "ps", "-aq", "--filter", "label=org.forge-web.project"],
        capture_output=True,
        text=True,
    ).stdout.split()
    if names:
        subprocess.run(["docker", "rm", "-f", *names], capture_output=True)


@needs_docker[0]
@needs_docker[1]
async def test_a_chat_survives_a_server_restart(docker_data: Path) -> None:
    script = fake_script(call("write_file", path="a.txt", content="A\n"), {"text": "Done."})
    settings = dev_settings(docker_data, script, isolation="docker", image=IMAGE)
    with LiveServer(settings) as first:
        async with httpx.AsyncClient(
            base_url=first.url, headers={"Cookie": first.cookie}, timeout=120
        ) as api:
            project = (await api.post("/api/projects", json={"name": "Docker"})).json()
            chat = (
                await api.post(f"/api/projects/{project['id']}/chats", json={"mode": "ask"})
            ).json()
            await api.post(f"/api/chats/{chat['id']}/messages", json={"text": "write a.txt"})
            for _ in range(600):
                state = (await api.get(f"/api/chats/{chat['id']}")).json()["state"]
                if state == "waiting":
                    break
                await asyncio.sleep(0.1)
            assert state == "waiting"
    # The first server is gone; its container (and the waiting chat in it) is not.
    with LiveServer(settings) as second:
        async with httpx.AsyncClient(
            base_url=second.url, headers={"Cookie": second.cookie}, timeout=120
        ) as api:
            pending: list[dict[str, Any]] = []
            for _ in range(300):
                pending = (await api.get(f"/api/chats/{chat['id']}")).json()["live"]["pending"]
                if pending:
                    break
                await asyncio.sleep(0.1)
            assert pending and pending[0]["payload"]["call"]["name"] == "write_file"
            answer = {"request_id": pending[0]["id"], "answer": {"allow": True}}
            assert (await api.post(f"/api/chats/{chat['id']}/answer", json=answer)).json()[
                "accepted"
            ]
            for _ in range(600):
                if (await api.get(f"/api/chats/{chat['id']}")).json()["state"] == "idle":
                    break
                await asyncio.sleep(0.1)
            events = (await api.get(f"/api/chats/{chat['id']}/events")).json()["items"]
            kinds = [e["item"]["type"] for e in events]
            assert kinds.count("request") == 1 and "turn" in kinds
            assert [e["seq"] for e in events] == list(range(1, len(events) + 1))
            read = await second.services.runs.call(project["id"], "fs.read", {"path": "a.txt"})
            assert read["text"] == "A\n"


@needs_docker[0]
@needs_docker[1]
async def test_programs_reach_only_allowed_hosts_through_the_egress_proxy() -> None:
    from forge_web.egress import Egress, EgressPolicy

    driver = DockerDriver(SandboxSettings(image=IMAGE, cpus=1, memory="1g"), egress_port=47102)
    egress = Egress(EgressPolicy(["pypi.org"]))
    client = SandboxClient(await driver.connect("dtest-e"), targets={"egress": egress.connect})
    await client.start()
    try:
        await client.call("forward.listen", {"target": "egress", "port": 47102})
        fetch = (
            "import urllib.request\n"
            "for url in ('https://pypi.org/simple/pip/', 'https://example.com/'):\n"
            " try:\n  print(urllib.request.urlopen(url, timeout=20).status)\n"
            " except Exception as e:\n  print('blocked', type(e).__name__)"
        )
        lines = await run_as_agent(client, fetch)
        assert lines[0] == "200", lines
        assert lines[1].startswith("blocked"), lines
    finally:
        await client.close()
        await egress.close()
        await driver.remove("dtest-e")
