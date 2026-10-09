"""Docker isolation: the container settings (offline) and, with a Docker daemon, the real thing."""

import asyncio
import json
import os
import subprocess
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from forge_web.containers.docker import DockerDriver, size_bytes
from forge_web.containers.driver import SandboxError
from forge_web.quotas import MB, enforce_disk
from forge_web.sandbox_cli import build_command, source_root
from forge_web.sandbox_client import SandboxClient
from forge_web.settings import SandboxSettings
from support import LiveServer, call, dev_settings, fake_script, remove_docker_projects

IMAGE = os.environ.get("FORGE_WEB_TEST_IMAGE", "forge-web-sandbox:dev")


def test_run_arguments_harden_the_container() -> None:
    driver = DockerDriver(SandboxSettings(image="img:1", cpus=1.5, memory="2g", pids=256))
    args = driver.run_args("abc123", "runsc")
    joined = " ".join(args)
    assert args[-1] == "img:1"
    for expected in (
        "--network none", "--read-only", "--cap-drop ALL", "--security-opt no-new-privileges",
        "--cpus 1.5", "--memory 2g", "--pids-limit 256", "--runtime runsc", "--init",
        "--pull never",
        "source=forge-web-abc123-workspace,target=/workspace",
        "source=forge-web-abc123-home,target=/home/forge",
    ):  # fmt: skip
        assert expected in joined, expected
    added = [args[i + 1] for i, a in enumerate(args) if a == "--cap-add"]
    assert set(added) == {"CHOWN", "DAC_OVERRIDE", "FOWNER", "SETUID", "SETGID", "KILL"}
    assert "--runtime" not in driver.run_args("abc123", None)
    assert "--privileged" not in joined and "docker.sock" not in joined


def test_a_folder_path_cannot_add_mount_options() -> None:
    driver = DockerDriver(SandboxSettings())
    driver.folders["abc123"] = Path("/srv/a,type=volume,source=forge-web-other-workspace")
    with pytest.raises(SandboxError, match="comma"):
        driver.workspace_mount("abc123")


async def test_disk_use_is_read_from_docker_itself(monkeypatch: pytest.MonkeyPatch) -> None:
    assert [size_bytes(t) for t in ("0B", "3MB", "1.5GB", "4.096kB", "7 B", "lots")] == [
        0, 3_000_000, 1_500_000_000, 4096, 7, None,
    ]  # fmt: skip
    volumes = [
        {"Name": "forge-web-abc123-workspace", "Size": "3MB"},
        {"Name": "forge-web-abc123-home", "Size": "1MB"},
        {"Name": "forge-web-other-workspace", "Size": "nonsense"},
        {"Name": "someone-elses-volume", "Size": "9GB"},
        "junk",
    ]
    answers = iter([(0, json.dumps({"Volumes": volumes}), ""), (1, "", "no daemon")])

    async def fake_docker(*args: str, **kwargs: Any) -> tuple[int, str, str]:
        assert args[:2] == ("system", "df")
        return next(answers)

    driver = DockerDriver(SandboxSettings())
    monkeypatch.setattr(driver, "docker", fake_docker)
    assert await driver.disk_use() == {"abc123": 4_000_000}
    assert await driver.disk_use() == {}  # cannot measure: nothing is stopped for it


async def test_projects_over_their_quota_are_stopped() -> None:
    stopped: list[str] = []

    async def disk_use() -> dict[str, int]:
        return {"small": 1_000_000, "big": 30 * MB}

    async def stop(project_id: str) -> None:
        stopped.append(project_id)

    async def forget(project_id: str) -> None:
        pass

    services: Any = SimpleNamespace(
        driver=SimpleNamespace(disk_use=disk_use, stop=stop),
        runs=SimpleNamespace(forget_project=forget),
        settings=SimpleNamespace(quotas=SimpleNamespace(project_disk_mb=10)),
        host_disk_use={}, disk_use={"big": (0.0, 5)},
    )  # fmt: skip
    assert await enforce_disk(services) == ["big"] and stopped == ["big"]
    assert services.host_disk_use["big"] == 30 * MB and "big" not in services.disk_use
    services.settings.quotas.project_disk_mb = 0  # no quota: nothing stops
    assert await enforce_disk(services) == [] and stopped == ["big"]


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
    for project_id in ("dtest-a", "dtest-b", "dtest-c", "dtest-d", "dtest-t", "dtest-p", "dtest-u"):
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
async def test_docker_measures_what_the_agent_wrote(driver: DockerDriver) -> None:
    client = await client_for(driver, "dtest-d")
    try:
        wrote = "open('/workspace/blob', 'wb').write(b'x' * 3_000_000); print('ok')"
        assert await run_as_agent(client, wrote) == ["ok"]
    finally:
        await client.close()
    assert (await driver.disk_use())["dtest-d"] >= 3_000_000


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


@needs_docker[0]
@needs_docker[1]
async def test_a_rebuilt_image_reaches_projects_at_their_next_start(driver: DockerDriver) -> None:
    tag = "forge-web-sandbox:test-update"
    subprocess.run(["docker", "tag", IMAGE, tag], check=True, capture_output=True)
    driver.settings.image = tag
    try:
        client = await client_for(driver, "dtest-u")
        await client.call("fs.write", {"path": "keep.txt", "text": "my work"})
        await client.close()
        first = (await driver.inspect("dtest-u") or {})["Image"]
        newer = f"FROM {IMAGE}\nLABEL org.forge-web.test=newer-forge\n"  # a rebuilt image
        subprocess.run(["docker", "build", "-q", "-t", tag, "-"], input=newer, text=True,
                       check=True, capture_output=True)  # fmt: skip
        await driver.ensure("dtest-u")
        assert (await driver.inspect("dtest-u") or {})["Image"] == first  # running: left alone
        await driver.stop("dtest-u")
        again = await client_for(driver, "dtest-u")
        try:
            assert (await again.call("fs.read", {"path": "keep.txt"}))["text"] == "my work"
        finally:
            await again.close()
        assert (await driver.inspect("dtest-u") or {})["Image"] != first  # the new image
    finally:
        await driver.remove("dtest-u")
        subprocess.run(["docker", "rmi", "--force", tag], capture_output=True)


@pytest.fixture
def docker_data(tmp_path: Path) -> Iterator[Path]:
    data = tmp_path / "data"
    yield data
    remove_docker_projects(data)


@needs_docker[0]
@needs_docker[1]
async def test_a_chat_survives_a_server_restart(docker_data: Path) -> None:
    script = fake_script(call("write_file", path="a.txt", content="A\n"), {"text": "Done."})
    settings = dev_settings(docker_data, script, isolation="docker", image=IMAGE)
    with LiveServer(settings) as first:
        async with httpx.AsyncClient(
            base_url=first.url, headers=first.headers(), timeout=120
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
            base_url=second.url, headers=second.headers(), timeout=120
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


async def command_output(client: SandboxClient, *argv: str) -> str:
    """What a command run as the agent prints (stripped)."""
    code = (
        f"import subprocess; print(subprocess.run({list(argv)!r}, capture_output=True, "
        "text=True).stdout.strip() or 'none')"
    )
    return "\n".join(await run_as_agent(client, code))


@needs_docker[0]
@needs_docker[1]
async def test_a_git_job_pushes_from_its_own_container(driver: DockerDriver) -> None:
    from forge_web.containers.gitjob import GitJob

    client = await client_for(driver, "dtest-c")
    try:
        await client.call("git.init")
        await client.call("fs.write", {"path": "a.txt", "text": "a\n"})
        await client.call("git.stage", {"paths": ["."]})
        await client.call("git.commit", {"message": "first", "name": "Ada", "email": "a@x.y"})
        await command_output(client, "git", "init", "-q", "--bare", "/workspace/remote.git")
        await client.call("git.bundle_out", {"branch": "main"})
        job = GitJob(
            "push", "file:///workspace/remote.git", "main", "main", local_remotes=True,
            credentials="https://ada:ghp_secret@github.com",
        )  # fmt: skip
        code, output = await driver.run_git_job("dtest-c", job)
        assert code == 0, output
        remote_log = ["git", "--git-dir", "/workspace/remote.git", "log", "--format=%s", "main"]
        assert await command_output(client, *remote_log) == "first"
        grep = ["grep", "-r", "-l", "ghp_secret", "/workspace", "/home/forge"]
        assert await command_output(client, *grep) == "none"  # the token stayed in the job
    finally:
        await client.close()


@needs_docker[0]
@needs_docker[1]
async def test_a_git_job_clones_into_a_project(driver: DockerDriver) -> None:
    from forge_web.containers.gitjob import GitJob

    client = await client_for(driver, "dtest-d")
    try:
        await client.call("git.init")
        script = (
            "git init -q -b trunk /workspace/up && cd /workspace/up && echo hi > hello.txt"
            " && git add . && git -c user.name=U -c user.email=u@x.y commit -q -m init"
        )
        started = await client.call("procs.start", {"command": script})
        for _ in range(100):
            if not (await client.call("procs.output", {"id": started["id"]}))["running"]:
                break
            await asyncio.sleep(0.05)
        job = GitJob("clone", "file:///workspace/up", "main", "", local_remotes=True)
        code, output = await driver.run_git_job("dtest-d", job)
        assert code == 0, output
        cloned = await client.call("git.bundle_clone", {"url": "https://example.com/up.git"})
        assert cloned == {"branch": "trunk"}
        assert (await client.call("fs.read", {"path": "hello.txt"}))["text"] == "hi\n"
    finally:
        await client.close()


@needs_docker[0]
@needs_docker[1]
async def test_a_terminal_runs_as_the_project_user(driver: DockerDriver) -> None:
    client = await client_for(driver, "dtest-t")
    try:
        term = await client.call("pty.create", {"cols": 80, "rows": 24})
        channel = await client.open("pty", {"id": term["id"]})
        await channel.send(b"echo who-$(id -u)-$(pwd); exit 7\n")
        seen = b""
        while data := await asyncio.wait_for(channel.read(), 15):
            seen += data
        assert b"who-1000-/workspace" in seen  # not root, in the workspace
        assert b"exited with code 7" in seen
    finally:
        await client.close()


@needs_docker[0]
@needs_docker[1]
async def test_a_preview_reaches_a_server_in_the_container(driver: DockerDriver) -> None:
    import httpcore

    from forge_web.preview_upstream import SandboxBackend

    client = await client_for(driver, "dtest-p")
    try:
        await client.call("fs.write", {"path": "index.html", "text": "<h1>from the box</h1>"})
        server = "python3 -m http.server 8000 --bind 127.0.0.1"
        await client.call("procs.start", {"command": server})

        async def open_port(port: int) -> Any:
            return await client.open("connect", {"port": port})

        backend = SandboxBackend(open_port)
        for _ in range(100):
            if 8000 in [p["port"] for p in await client.call("ports.list")]:
                break
            await asyncio.sleep(0.1)
        origin = httpcore.Origin(b"http", b"localhost", 8000)
        async with httpcore.AsyncHTTPConnection(origin, network_backend=backend) as connection:
            response = await connection.request(
                "GET", "http://localhost:8000/index.html", headers={"Host": "localhost:8000"}
            )
        assert response.status == 200 and b"from the box" in response.content
    finally:
        await client.close()
