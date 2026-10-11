"""The host's edge, firewall and doctor (W25b): only live apps get a certificate, the legal
pages are the edge's on every app, the firewall keeps app containers off private networks,
other apps, the host and port 25, and `doctor` names every missing piece with its fix.

Docker is the stand-in from `docker_standin.py`, Caddy an httpx mock and iptables a recorder,
so these tests run everywhere."""

import ipaddress
import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import pytest

from docker_standin import JOB_ID, FakeDocker, release_job, runner_for, tarball
from forge_hostworker import firewall
from forge_hostworker.cli import main
from forge_hostworker.docker import HostError, container_owner, mapped_id
from forge_hostworker.doctor import TEST_IMAGE, doctor, report, worker_check
from forge_hostworker.edge import LEGAL_PATHS, Edge, LiveApp, Upstream, app_host, caddy_config
from forge_hostworker.runner import HostRunner
from forge_hostworker.wire import HostJob

DOMAIN = "apps.example"
LEGAL = "https://forge.example.com/legal"


class Caddy:
    """Caddy's admin API: remembers every configuration it was given."""

    def __init__(self, up: bool = True) -> None:
        self.up = up
        self.loaded: list[dict[str, Any]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if not self.up:
            raise httpx.ConnectError("connection refused", request=request)
        if request.method == "POST" and request.url.path == "/load":
            self.loaded.append(json.loads(request.content))
        return httpx.Response(200, json={})

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self)


def hosts_in(config: dict[str, Any]) -> list[str]:
    """The hosts a Caddy configuration serves."""
    routes = config["apps"]["http"]["servers"]["apps"]["routes"]
    return [host for route in routes for host in route["match"][0]["host"]]


async def ask(port: int, target: str, method: str = "GET") -> int:
    """Caddy's TLS question to the worker; the status it gets."""
    async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}") as client:
        return (await client.request(method, target)).status_code


async def edge_with_live_shop(tmp_path: Path) -> tuple[Edge, HostRunner, FakeDocker, Caddy]:
    """An edge whose runner has made release 1 of `shop` live in staging."""
    docker, caddy = FakeDocker(), Caddy()
    runner = runner_for(tmp_path, docker)
    edge = Edge(runner, docker, DOMAIN, LEGAL, transport=caddy.transport())
    runner.on_change = edge.changed
    assert (await runner.run(release_job(), tarball(tmp_path / "s.tgz"), {})).ok
    return edge, runner, docker, caddy


async def test_only_live_apps_get_a_certificate(tmp_path: Path) -> None:
    edge, runner, _, caddy = await edge_with_live_shop(tmp_path)
    assert hosts_in(caddy.loaded[-1]) == ["shop.staging.apps.example"]  # loaded after the job
    server = await edge.serve_ask(port=0)
    port = server.sockets[0].getsockname()[1]
    try:
        assert await ask(port, "/ask?domain=shop.staging.apps.example") == 200
        assert await ask(port, "/ask?domain=SHOP.staging.apps.example.") == 200
        assert await ask(port, "/ask?domain=shop.apps.example") == 404  # not in production
        assert await ask(port, "/ask?domain=evil.example") == 404
        assert await ask(port, "/ask") == 404
        assert await ask(port, "/other?domain=shop.staging.apps.example") == 404
        assert await ask(port, "/ask?domain=shop.staging.apps.example", "POST") == 404
        stop = HostJob(id=JOB_ID, kind="stop", app="shop", environment="staging")
        assert (await runner.run(stop, None, {})).ok
        assert hosts_in(caddy.loaded[-1]) == []
        assert await ask(port, "/ask?domain=shop.staging.apps.example") == 404
    finally:
        server.close()
        await edge.close()


async def test_a_stopped_container_is_not_served(tmp_path: Path) -> None:
    edge, _, docker, _ = await edge_with_live_shop(tmp_path)
    docker.containers["forge-shop-staging-api-r1"]["running"] = False
    assert await edge.refresh() == []
    assert not edge.allowed("shop.staging.apps.example")
    await edge.close()


async def test_caddy_being_away_does_not_fail_a_job(tmp_path: Path) -> None:
    docker, caddy = FakeDocker(), Caddy(up=False)
    runner = runner_for(tmp_path, docker)
    edge = Edge(runner, docker, DOMAIN, LEGAL, transport=caddy.transport())
    runner.on_change = edge.changed
    assert (await runner.run(release_job(), tarball(tmp_path / "s.tgz"), {})).ok
    caddy.up = True
    await edge.changed()
    assert hosts_in(caddy.loaded[-1]) == ["shop.staging.apps.example"]
    await edge.close()


def test_host_names_per_environment() -> None:
    assert app_host("shop", "production", "Apps.Example.") == "shop.apps.example"
    assert app_host("shop", "staging", "apps.example") == "shop.staging.apps.example"


def subroutes(config: dict[str, Any], host: str) -> list[dict[str, Any]]:
    """The routes inside one host's route, in Caddy's order."""
    for route in config["apps"]["http"]["servers"]["apps"]["routes"]:
        if route["match"][0]["host"] == [host]:
            return list(route["handle"][-1]["routes"])
    raise AssertionError(f"{host} is not served")


def test_the_edges_legal_pages_win_over_the_apps_routes() -> None:
    app = LiveApp(
        "shop",
        "production",
        "shop.apps.example",
        (Upstream("/api", "10.88.1.2:8000"), Upstream("/", "10.88.1.3:8080")),
    )
    config = caddy_config([app], LEGAL, ask_port=47201)
    routes = subroutes(config, "shop.apps.example")
    legal, api, web = routes
    assert legal["match"] == [{"path": list(LEGAL_PATHS)}]
    assert {"/impressum", "/datenschutz", "/melden"} <= set(LEGAL_PATHS)
    proxy = legal["handle"][0]
    assert proxy["upstreams"] == [{"dial": "forge.example.com:443"}]
    assert proxy["transport"]["tls"] == {}
    assert proxy["rewrite"]["uri"] == "/legal/shop{http.request.uri.path}"
    assert proxy["headers"]["request"]["set"]["Host"] == ["forge.example.com"]
    assert all(route["terminal"] for route in routes)
    # The app's own routes come after, the longest first, and never cover the legal paths.
    assert api["match"] == [{"path": ["/api", "/api/*"]}]
    assert api["handle"][0]["upstreams"] == [{"dial": "10.88.1.2:8000"}]
    assert web["match"] == [{"path": ["/*"]}]
    tls = config["apps"]["tls"]["automation"]
    assert tls["on_demand"]["permission"]["endpoint"] == "http://127.0.0.1:47201/ask"
    assert config["admin"]["listen"] == "127.0.0.1:2019"


async def test_the_longest_route_comes_first_from_a_real_release(tmp_path: Path) -> None:
    edge, _, _, caddy = await edge_with_live_shop(tmp_path)
    routes = subroutes(caddy.loaded[-1], "shop.staging.apps.example")
    assert [r["match"][0]["path"][0] for r in routes] == ["/impressum", "/api", "/*"]
    await edge.close()


@dataclass(frozen=True)
class Packet:
    """A packet through the host, as far as the rules look at it."""

    src: str
    dst: str
    proto: str = "tcp"
    port: int = 443
    state: str = "NEW"
    bridged: bool = False  # within one Docker network (an app's services and its database)
    over_rate: bool = False  # past the per-container rate of new connections


def options(rule: str) -> dict[str, str]:
    """A rule's options by name (values joined), e.g. {"-s": "10.88.0.0/16", "-j": "DROP"}."""
    found: dict[str, str] = {}
    words = rule.split()[2:]
    for i, word in enumerate(words):
        if word.startswith("-"):
            value = words[i + 1] if i + 1 < len(words) and not words[i + 1].startswith("-") else ""
            found[word] = value
    return found


def matches(rule: dict[str, str], packet: Packet) -> bool:
    """Whether one rule matches the packet."""
    inside = ipaddress.ip_address

    def net(name: str, address: str) -> bool:
        return name not in rule or inside(address) in ipaddress.ip_network(rule[name])

    if not (net("-s", packet.src) and net("-d", packet.dst)):
        return False
    if "-p" in rule and rule["-p"] != packet.proto:
        return False
    if "--dport" in rule and int(rule["--dport"]) != packet.port:
        return False
    if "--dports" in rule and str(packet.port) not in rule["--dports"].split(","):
        return False
    if "--ctstate" in rule and packet.state not in rule["--ctstate"].split(","):
        return False
    if "--physdev-is-bridged" in rule and not packet.bridged:
        return False
    return "--hashlimit-above" not in rule or packet.over_rate


def verdict(lines: list[str], packet: Packet) -> str:
    """What the chain does with the packet: RETURN (Docker's own rules go on) or DROP."""
    for line in lines:
        rule = options(line)
        if matches(rule, packet):
            return rule["-j"]
    return "RETURN"


APP, OTHER_APP, BUILD = "10.88.1.2", "10.88.2.2", "10.89.0.5"
EGRESS_CASES = [
    (Packet(APP, "93.184.216.34"), "RETURN"),  # the internet
    (Packet(APP, "169.254.169.254", port=80), "DROP"),  # cloud metadata
    (Packet(APP, "100.100.100.200", port=80), "DROP"),  # cloud metadata (carrier-grade NAT)
    (Packet(APP, "10.0.0.5"), "DROP"),  # a private network
    (Packet(APP, "192.168.1.1", port=22), "DROP"),
    (Packet(APP, "172.17.0.1", port=2375), "DROP"),  # Docker's default bridge
    (Packet(APP, OTHER_APP, port=5432), "DROP"),  # another app (routed between networks)
    (Packet(APP, "10.88.1.3", port=5432, bridged=True), "RETURN"),  # its own database
    (Packet(APP, "93.184.216.34", port=25), "DROP"),  # no mail but through the relay
    (Packet(APP, "93.184.216.34", port=587), "RETURN"),
    (Packet(APP, "93.184.216.34", port=3333), "DROP"),  # a mining pool
    (Packet(APP, "93.184.216.34", over_rate=True), "DROP"),  # scanning
    (Packet(APP, "10.0.0.5", state="ESTABLISHED"), "RETURN"),  # an answer to the edge
    (Packet(BUILD, "104.16.0.1"), "RETURN"),  # a package registry
    (Packet(BUILD, "8.8.8.8", proto="udp", port=53), "RETURN"),
    (Packet(BUILD, "93.184.216.34", port=22), "DROP"),  # builds reach only DNS and HTTP(S)
    (Packet(BUILD, "10.0.0.5"), "DROP"),
    (Packet(BUILD, APP, port=8000), "DROP"),  # no app from a build
]


@pytest.mark.parametrize(("packet", "expected"), EGRESS_CASES)
def test_the_firewall_for_each_case(packet: Packet, expected: str) -> None:
    assert verdict(firewall.rules(), packet) == expected


def test_containers_cannot_start_talking_to_the_host() -> None:
    gateway = "10.88.1.1"
    assert verdict(firewall.host_rules(), Packet(APP, gateway, port=22)) == "DROP"
    assert verdict(firewall.host_rules(), Packet(BUILD, "10.89.0.1", port=2019)) == "DROP"
    assert verdict(firewall.host_rules(), Packet(APP, gateway, state="ESTABLISHED")) == "RETURN"
    assert verdict(firewall.host_rules(), Packet("203.0.113.9", gateway, port=22)) == "RETURN"


def test_allowed_addresses_open_only_themselves() -> None:
    allow = ("10.1.2.3",)
    assert verdict(firewall.rules(allow), Packet(APP, "10.1.2.3", port=8025)) == "RETURN"
    assert verdict(firewall.rules(allow), Packet(APP, "10.1.2.4", port=8025)) == "DROP"


def test_applying_loads_both_chains_and_jumps_once() -> None:
    calls: list[tuple[list[str], str]] = []
    present: set[str] = set()

    def run(argv: list[str], stdin: str) -> int:
        calls.append((argv, stdin))
        if argv[:2] == ["iptables", "-I"]:
            present.add(argv[2])
        return 1 if argv[:2] == ["iptables", "-C"] and argv[2] not in present else 0

    first = firewall.apply(run=run)
    assert first == [
        "iptables-restore --noflush",
        "iptables -I DOCKER-USER -j FORGE-EGRESS",
        "iptables -I INPUT -j FORGE-HOST",
    ]
    script = calls[0][1]
    assert script.startswith("*filter\n:FORGE-EGRESS - [0:0]\n:FORGE-HOST - [0:0]\n")
    assert script.rstrip().endswith("COMMIT") and "-F FORGE-EGRESS" in script
    assert firewall.apply(run=run) == ["iptables-restore --noflush"]  # jumps exist already
    with pytest.raises(OSError, match="iptables-restore"):
        firewall.apply(run=lambda argv, stdin: 1)


def test_the_firewall_command_prints_the_rules(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["firewall", "--allow", "10.1.2.3"]) == 0
    out = capsys.readouterr().out
    assert "-A FORGE-EGRESS -s 10.88.0.0/16 -d 10.1.2.3 -j RETURN" in out
    assert "-A FORGE-HOST -s 10.88.0.0/16 -j DROP" in out


def test_the_edge_needs_both_its_options(tmp_path: Path) -> None:
    with pytest.raises(SystemExit) as stopped:
        main(["run", "--server", "https://f.example", "--data", str(tmp_path),
              "--apps-domain", DOMAIN])  # fmt: skip
    assert stopped.value.code == 2


def ready_host(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A data folder with a key and a token."""
    monkeypatch.delenv("FORGE_HOST_TOKEN", raising=False)
    data = tmp_path / "host"
    data.mkdir()
    (data / "host.key").write_bytes(b"k" * 32)
    (data / "token").write_text("fhw_x", encoding="utf-8")
    return data


async def test_a_ready_host_passes_every_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    docker = FakeDocker(userns=True)
    await docker("network", "create", "--driver", "bridge", "--subnet", "10.89.0.0/24",
                 "forge-build")  # fmt: skip
    checks = await doctor(
        docker,
        ready_host(tmp_path, monkeypatch),
        transport=Caddy().transport(),
        run=lambda argv, stdin: 0,
        root=True,
        min_free_gb=0,
    )
    assert all(c.ok for c in checks), report(checks)
    names = {c.name for c in checks}
    assert {"gVisor", "user namespaces", "sandbox", "firewall", "Caddy", "disk"} <= names
    sandbox = next(c for c in docker.runs() if TEST_IMAGE in c)
    assert {"--runtime=runsc", "--read-only", "--cap-drop"} <= set(sandbox)


async def test_doctor_names_every_missing_piece_with_its_fix(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("FORGE_HOST_TOKEN", raising=False)
    docker = FakeDocker(userns=False)
    docker.fail.add(TEST_IMAGE)
    checks = await doctor(
        docker,
        tmp_path / "empty",
        transport=Caddy(up=False).transport(),
        run=lambda argv, stdin: 1,
        root=True,
        min_free_gb=10**9,
    )
    failed = {c.name: c.fix for c in checks if not c.ok}
    assert set(failed) == {
        "user namespaces",
        "sandbox",
        "build network",
        "firewall",
        "Caddy",
        "host key",
        "token",
        "disk",
    }, report(checks)
    assert all(failed.values())
    assert "userns-remap" in failed["user namespaces"]
    assert failed["firewall"] == "sudo forge-host-worker firewall --apply"
    assert "keygen" in failed["host key"]
    text = report(checks)
    assert "FAIL sandbox" in text and "fix: forge-host-worker keygen" in text


async def test_doctor_without_gvisor_says_how_to_install_it(tmp_path: Path) -> None:
    checks = await doctor(
        FakeDocker(runtimes=("runc",)),
        tmp_path,
        transport=Caddy().transport(),
        run=lambda argv, stdin: 0,
        root=True,
        min_free_gb=0,
    )
    gvisor = next(c for c in checks if c.name == "gVisor")
    assert not gvisor.ok and "runsc install" in gvisor.fix


def test_under_userns_the_worker_must_be_root() -> None:
    assert worker_check(userns=False, root=False).ok
    assert worker_check(userns=True, root=True).ok
    refused = worker_check(userns=True, root=False)
    assert not refused.ok and "root" in refused.fix


async def test_release_folders_go_to_the_containers_host_user(tmp_path: Path) -> None:
    etc = tmp_path / "etc"
    etc.mkdir()
    (etc / "subuid").write_text("someone:100000:65536\ndockremap:231072:65536\n")
    (etc / "subgid").write_text("dockremap:231072:65536\n")
    assert mapped_id(1000, (etc / "subuid").read_text()) == 232072
    owner = await container_owner(FakeDocker(userns=True), "1000:1000", root=True, etc=etc)
    assert owner == (232072, 232072)
    assert await container_owner(FakeDocker(), "1000:1000", root=True, etc=etc) == (1000, 1000)
    assert await container_owner(FakeDocker(), "1500:1500", root=False, etc=etc) is None
    with pytest.raises(HostError, match="root"):
        await container_owner(FakeDocker(userns=True), "1500:1500", root=False, etc=etc)
    with pytest.raises(HostError):
        mapped_id(1000, "someone:100000:65536\n")


@pytest.mark.skipif(sys.platform == "win32", reason="file owners are POSIX")
async def test_the_runner_hands_the_release_over(tmp_path: Path) -> None:
    own = (os.getuid(), os.getgid())
    docker = FakeDocker()
    runner = HostRunner(docker, tmp_path / "host", user="1500:1500", owner=own)
    runner.health = runner_for(tmp_path, docker).health
    assert (await runner.run(release_job(), tarball(tmp_path / "s.tgz"), {})).ok
    folder = tmp_path / "host" / "apps" / "shop-staging" / "releases" / "1" / "server" / "app"
    assert (folder.stat().st_uid, folder.stat().st_gid) == own


def test_app_names_the_edge_needs_are_reserved() -> None:
    with pytest.raises(ValueError, match="reserved"):
        HostJob(id=JOB_ID, kind="stop", app="staging", environment="production")
    with pytest.raises(ValueError, match="reserved"):
        release_job(app="www")
