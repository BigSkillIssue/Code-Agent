"""Forge's WireGuard network (W65): every node gets an address in 10.90.0.0/24 (Forge Web is .1),
freed again when the node goes; a fixed policy says who may talk to whom on `forge0`; a node
learns its view (`GET /api/host/mesh`) when the poll answer's revision changes, writes
`forge0.conf`, brings the interface up or syncs it and drops everything the policy does not allow;
`forge-web mesh config` prints Forge Web's own configuration. Private keys never reach a log, an
audit entry or an error message."""

import asyncio
import ipaddress
import json
import logging
import sys
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from forge_hostworker import firewall
from forge_hostworker.client import HostClient
from forge_hostworker.mesh import MeshNode, node_config
from forge_hostworker.mesh_wire import (
    FORGE_WEB_IP,
    MeshPeer,
    MeshRule,
    MeshView,
    revision_of,
    wg_quick_config,
)
from forge_hostworker.wire import PollAnswer, endpoint_ok
from forge_web import cli as web_cli
from forge_web.db.engine import Database
from forge_web.db.hosting_models import HostServer
from forge_web.db.models import AuditEntry
from forge_web.hosting import deploy_api, hosts, mesh
from forge_web.hosting.hosts import create_host
from forge_web.hosting.mesh import (
    assign_address,
    forge_web_config,
    forge_web_keys,
    free_address,
    mesh_policy,
    peers_for,
    release_address,
    rules_for,
)
from forge_web.vault import Vault, load_master_key
from host_standin import HostStandIn
from mesh_standin import (
    FakeWireGuard,
    host_named,
    key32,
    labels,
    member,
    mesh_client,
    node,
    peer,
    revision,
    stored,
    worker_data,
)
from support import LiveServer
from test_hosting_deploy import settings_for

POSIX = sys.platform != "win32"
WEB = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def short_polls(monkeypatch: pytest.MonkeyPatch) -> None:
    """Hosts ask again every second, so a server stops without waiting for a long poll."""
    monkeypatch.setattr(deploy_api, "POLL_WAIT_S", 1.0)
    monkeypatch.setattr(hosts, "REOFFER_AFTER_S", 2.0)


@pytest.fixture
def server(tmp_path: Path) -> Iterator[LiveServer]:
    with LiveServer(settings_for(tmp_path / "data")) as live:
        yield live


@pytest.fixture
async def db(tmp_path: Path) -> AsyncIterator[Database]:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'mesh.db'}")
    await database.migrate()
    yield database
    await database.close()


# -- addresses ---------------------------------------------------------------------------


async def test_addresses_are_unique_and_reused_after_removal(db: Database) -> None:
    made = [await node(db, f"n{i}", ["apps"]) for i in range(12)]
    given = await asyncio.gather(*(assign_address(db, host.id) for host, _ in made))
    assert len(set(given)) == 12 and None not in given  # side by side, still no duplicate
    subnet = ipaddress.ip_network("10.90.0.0/24")
    for address in given:
        assert address is not None and ipaddress.ip_address(address) in subnet
        assert address not in (FORGE_WEB_IP, "10.90.0.0", "10.90.0.255")
    first = made[0][0].id
    assert await assign_address(db, first) == given[0]  # asking again keeps it
    assert sorted(given, key=ipaddress.ip_address)[0] == "10.90.0.2"
    gone = made[3][0].id
    freed = (await stored(db, gone)).mesh_ip
    await release_address(db, gone)
    assert (await stored(db, gone)).mesh_ip is None
    newcomer, _ = await node(db, "newcomer", ["db"])
    assert await assign_address(db, newcomer.id) == freed  # the lowest free one again
    no_wg, _ = await node(db, "old", ["apps"], wg=False)
    assert await assign_address(db, no_wg.id) is None  # no WireGuard key: not in the network
    async with db.session() as session, session.begin():
        twin = await session.get(HostServer, no_wg.id)
        assert twin is not None
        twin.mesh_ip = freed
        with pytest.raises(IntegrityError):  # the database itself refuses a duplicate
            await session.flush()


def test_a_full_network_has_no_free_address() -> None:
    every = {f"10.90.0.{i}" for i in range(2, 255)}
    assert free_address(every) is None
    assert free_address(every - {"10.90.0.77"}) == "10.90.0.77"
    assert free_address(set()) == "10.90.0.2"


# -- the policy ----------------------------------------------------------------------------


def test_the_policy_table() -> None:
    table = {(r.source, r.target, r.protocol, r.port_from, r.port_to) for r in mesh_policy()}
    assert table == {
        ("apps", "db", "tcp", 5432, 5532),
        ("forge-web", "ai", "tcp", 11434, 11434),
        ("forge-web", "ai", "tcp", 8000, 8000),
    }
    assert all(r.target != "storage" for r in mesh_policy())  # storage accepts nothing
    assert all(r.why for r in mesh_policy())


def test_who_may_talk_to_whom_per_role() -> None:
    apps, db, ai = member("a", ["apps"], "10.90.0.2"), member("d", ["db"], "10.90.0.3"), \
        member("i", ["ai"], "10.90.0.4")  # fmt: skip
    storage, both = member("s", ["storage"], "10.90.0.5"), member("b", ["apps", "db"], "10.90.0.6")
    everyone = [apps, db, ai, storage, both]

    def rules(host: HostServer) -> set[tuple[str, str, int, int]]:
        return {(r.source, r.protocol, r.port_from, r.port_to) for r in rules_for(host, everyone)}

    def peers(host: HostServer) -> list[str]:
        return [p.label for p in peers_for(host, everyone, key32("forge"), None)]

    assert rules(db) == {("10.90.0.2", "tcp", 5432, 5532), ("10.90.0.6", "tcp", 5432, 5532)}
    assert rules(both) == {("10.90.0.2", "tcp", 5432, 5532)}  # not from itself
    assert rules(ai) == {(FORGE_WEB_IP, "tcp", 11434, 11434), (FORGE_WEB_IP, "tcp", 8000, 8000)}
    assert rules(storage) == set() and rules(apps) == set()
    assert peers(storage) == ["forge-web"] and peers(ai) == ["forge-web"]
    assert peers(apps) == ["forge-web", "d", "b"]
    assert peers(db) == ["forge-web", "a", "b"]
    first = peers_for(apps, everyone, key32("forge"), "forge.example.com:51820")[0]
    assert (first.allowed_ip, first.endpoint) == (f"{FORGE_WEB_IP}/32", "forge.example.com:51820")
    assert all(p.keepalive_s == 25 for p in peers_for(db, everyone, key32("forge"), None))
    off = member("o", ["apps"], "10.90.0.7")
    off.enabled = False
    assert [p.label for p in peers_for(db, [*everyone, off], key32("forge"), None)] == [
        "forge-web", "a", "b"]  # fmt: skip


# -- the rendered configuration ------------------------------------------------------------


def test_the_rendered_configuration() -> None:
    view = MeshView(address="10.90.0.3", peers=[peer("forge-web", FORGE_WEB_IP),
                    peer("a1b2", "10.90.0.2", "198.51.100.4:51820")])  # fmt: skip
    text = node_config(view, key32("secret"))
    assert text.splitlines()[:5] == [
        "# Forge's WireGuard network, written by forge-host-worker; changes are overwritten.",
        "[Interface]",
        "Address = 10.90.0.3/24",
        "ListenPort = 51820",
        f"PrivateKey = {key32('secret')}",
    ]
    assert text.count("[Peer]") == 2 and "PersistentKeepalive = 25" in text
    assert f"PublicKey = {key32('a1b2')}" in text and "AllowedIPs = 10.90.0.2/32" in text
    assert "Endpoint = 198.51.100.4:51820" in text and text.count("Endpoint") == 1
    with pytest.raises(ValueError) as refused:
        wg_quick_config("s3cr3t-value", "10.90.0.3", [])
    assert "s3cr3t-value" not in str(refused.value)
    assert revision_of(view) == revision_of(view.model_copy()) > 0
    assert revision_of(view) != revision_of(view.model_copy(update={"address": "10.90.0.4"}))


@pytest.mark.parametrize(
    "bad",
    [
        {"label": "x\nPostUp = rm -rf /"},
        {"label": "Forge Web"},
        {"public_key": "short"},
        {"allowed_ip": "10.91.0.2/32"},
        {"allowed_ip": "10.90.0.0/24"},
        {"allowed_ip": "10.90.0.255/32"},
        {"endpoint": "evil.example.com\n:51820"},
        {"endpoint": "203.0.113.4"},
    ],
)
def test_nothing_from_the_server_breaks_into_the_configuration(bad: dict[str, str]) -> None:
    fields = {"label": "ok", "public_key": key32("ok"), "allowed_ip": "10.90.0.9/32", **bad}
    with pytest.raises(ValueError):
        MeshPeer.model_validate(fields)


def test_views_and_rules_are_validated() -> None:
    assert not endpoint_ok("evil.example.com\n:51820") and not endpoint_ok("a.example.com :1")
    assert endpoint_ok("node.example.com:51820")
    for address in ("10.89.0.2", "10.90.0.1", "10.90.0.0", "300.1.1.1", "10.90.0.2/24"):
        with pytest.raises(ValueError):
            MeshView(address=address)
    with pytest.raises(ValueError):
        MeshRule(source="10.90.0.2", protocol="tcp", port_from=6000, port_to=5000)
    with pytest.raises(ValueError):
        MeshRule(source="192.168.1.2", protocol="tcp", port_from=1, port_to=2)
    assert PollAnswer.model_validate_json('{"job": null}').mesh_revision == 0  # older servers


async def test_forge_web_config_names_every_node(db: Database, tmp_path: Path) -> None:
    vault = Vault(load_master_key(tmp_path / "data"))
    settings = settings_for(tmp_path / "data")
    settings.server.public_url = "https://forge.example.com"
    apps, _ = await node(db, "apps-1", ["apps"], "198.51.100.4:51820")
    storage, _ = await node(db, "store-1", ["storage"])
    await node(db, "old", ["apps"], wg=False)
    text = await forge_web_config(db, vault, settings)
    private, public = forge_web_keys(vault)
    assert forge_web_keys(vault) == (private, public)  # derived, the same every time
    assert "Address = 10.90.0.1/24" in text and f"PrivateKey = {private}" in text
    assert "ListenPort = 51820" in text and public not in text
    assert text.count("[Peer]") == 2  # the one without a WireGuard key is not in the network
    for host in (apps, storage):
        row = await stored(db, host.id)
        assert row.mesh_ip is not None and f"AllowedIPs = {row.mesh_ip}/32" in text
        assert f"PublicKey = {row.wg_public_key}" in text
    assert "Endpoint = 198.51.100.4:51820" in text
    assert "PostUp = iptables -I INPUT -i forge0 -m conntrack --ctstate NEW -j DROP" in text
    assert "PostDown = iptables -D INPUT -i forge0 -m conntrack --ctstate NEW -j DROP" in text
    assert mesh.forge_web_endpoint(settings) == "forge.example.com:51820"
    settings.server.public_url = ""
    assert mesh.forge_web_endpoint(settings) is None


def test_forge_web_mesh_config_keeps_the_key_out_of_logs(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], caplog: pytest.LogCaptureFixture
) -> None:
    data = tmp_path / "web"

    async def prepare() -> None:
        database = Database(f"sqlite+aiosqlite:///{data / 'forge-web.db'}")
        data.mkdir(parents=True)
        await database.migrate()
        await node(database, "db-1", ["db"], "198.51.100.9:51820")
        await database.close()

    asyncio.run(prepare())
    caplog.set_level(logging.DEBUG)
    assert web_cli.main(["--data-dir", str(data), "mesh", "config"]) == 0
    shown = capsys.readouterr()
    private, _ = forge_web_keys(Vault(load_master_key(data)))
    assert f"PrivateKey = {private}" in shown.out and shown.out.count("[Peer]") == 1
    assert private not in shown.err and private not in caplog.text

    async def entries() -> list[str]:
        database = Database(f"sqlite+aiosqlite:///{data / 'forge-web.db'}")
        async with database.session() as session:
            rows = list(await session.scalars(select(AuditEntry)))
        await database.close()
        return [f"{row.action} {row.detail}" for row in rows]

    audited = asyncio.run(entries())
    assert any(entry.startswith("hosting.mesh_config_shown") for entry in audited)
    assert all(private not in entry for entry in audited)


# -- the firewall on forge0 ----------------------------------------------------------------


def test_firewall_rules_on_forge0_as_data() -> None:
    allowed = [
        MeshRule(source="10.90.0.2", protocol="tcp", port_from=5432, port_to=5532),
        MeshRule(source=FORGE_WEB_IP, protocol="tcp", port_from=11434, port_to=11434),
    ]
    assert firewall.mesh_rules(allowed) == [
        "-A FORGE-MESH -m conntrack --ctstate RELATED,ESTABLISHED -j RETURN",
        "-A FORGE-MESH -s 10.90.0.2/32 -p tcp --dport 5432:5532 -j RETURN",
        "-A FORGE-MESH -s 10.90.0.1/32 -p tcp --dport 11434 -j RETURN",
        "-A FORGE-MESH -j DROP",
    ]
    assert firewall.mesh_rules([]) == [
        "-A FORGE-MESH -m conntrack --ctstate RELATED,ESTABLISHED -j RETURN",
        "-A FORGE-MESH -j DROP",
    ]
    script = firewall.mesh_restore_script(allowed)
    assert script.startswith("*filter\n:FORGE-MESH - [0:0]\n-F FORGE-MESH\n")
    assert script.endswith("COMMIT\n") and "FORGE-EGRESS" not in script  # the apps' rules stay
    ran: list[tuple[list[str], str]] = []

    def run(argv: list[str], stdin: str) -> int:
        ran.append((argv, stdin))
        return 1 if argv[1] == "-C" else 0

    done = firewall.apply_mesh(allowed, run=run)
    assert ran[0] == (["iptables-restore", "--noflush"], script)
    assert done == ["iptables-restore --noflush",
                    "iptables -I INPUT -i forge0 -j FORGE-MESH",
                    "iptables -I FORWARD -i forge0 -j FORGE-MESH"]  # fmt: skip
    ran.clear()
    assert firewall.apply_mesh(allowed, run=lambda argv, stdin: 0) == [
        "iptables-restore --noflush"]  # fmt: skip
    with pytest.raises(OSError):
        firewall.apply_mesh(allowed, run=lambda argv, stdin: 1)


# -- the worker ----------------------------------------------------------------------------


async def test_the_worker_applies_the_config_and_reapplies_only_on_a_new_revision(
    server: LiveServer, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    db = server.services.db
    apps, token = await node(db, "apps-1", ["apps"])
    data, config = worker_data(tmp_path)
    wg = FakeWireGuard(config)
    mesh_node = MeshNode(data, config=config, run=wg)
    async with mesh_client(server, token) as http:
        first = await revision(http)
        assert first > 0
        assert await mesh_node.sync(http, first)
        firewall_first = ["iptables-restore --noflush", *["iptables -C", "iptables -I"] * 2]
        assert wg.commands() == [*firewall_first, "wg show", "wg-quick up"]
        text = config.read_text(encoding="utf-8")
        address = (await stored(db, apps.id)).mesh_ip
        assert f"Address = {address}/24" in text
        assert f"PrivateKey = {key32('node-private')}" in text and text.count("[Peer]") == 1
        if POSIX:
            assert config.stat().st_mode & 0o777 == 0o600
        wg.calls.clear()
        assert not await mesh_node.sync(http, first)  # the same revision: nothing to do
        assert await revision(http) == first and wg.calls == []
        await node(db, "db-1", ["db"], "198.51.100.9:51820")  # a peer of the apps node
        await node(db, "store-1", ["storage"])  # not one: nothing changes for apps-1
        await assign_address(db, (await host_named(db, "store-1")).id)
        second = await revision(http)
        assert second != first
        assert await mesh_node.sync(http, second)
        assert wg.commands()[-3:] == ["wg show", "wg-quick strip", "wg syncconf"]
        assert config.read_text().count("[Peer]") == 2 and "Endpoint = 198.51.100.9:51820" in (
            wg.synced)  # fmt: skip
        assert "Address" not in wg.synced
        if POSIX:
            assert wg.synced_mode == 0o600
        assert not list(config.parent.glob("*.strip*")) and not list(data.glob("*.strip*"))
    assert key32("node-private") not in caplog.text
    assert forge_web_keys(server.services.vault)[0] not in caplog.text


async def test_a_new_address_restarts_the_interface_and_failures_are_retried_later(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    data, config = worker_data(tmp_path)
    wg = FakeWireGuard(config)
    now = [0.0]
    mesh_node = MeshNode(data, config=config, run=wg, clock=lambda: now[0])
    view = MeshView(address="10.90.0.5", peers=[peer("forge-web", FORGE_WEB_IP)])
    mesh_node.apply(view)
    wg.calls.clear()
    mesh_node.apply(view.model_copy(update={"address": "10.90.0.6"}))
    assert wg.commands()[-3:] == ["wg show", "wg-quick down", "wg-quick up"]
    assert "Address = 10.90.0.6/24" in config.read_text()

    def answer(request: httpx.Request) -> httpx.Response:
        moved = view.model_copy(update={"address": "10.90.0.7"})
        return httpx.Response(200, content=moved.model_copy(
            update={"revision": revision_of(moved)}).model_dump_json())  # fmt: skip

    wg.fail = "wg-quick down"
    async with httpx.AsyncClient(base_url="http://forge", transport=httpx.MockTransport(answer)) \
            as http:  # fmt: skip
        assert not await mesh_node.sync(http, 41)
        assert "WireGuard" in caplog.text and key32("node-private") not in caplog.text
        tries = len(wg.calls)
        assert not await mesh_node.sync(http, 41) and len(wg.calls) == tries  # not at once
        now[0] += 301
        wg.fail = ""
        assert await mesh_node.sync(http, 41)
    (data / "wg.key").unlink()
    with pytest.raises(OSError) as missing:
        mesh_node.apply(view.model_copy(update={"address": "10.90.0.8"}))
    assert "wg.key" in str(missing.value)


async def test_a_worker_without_an_address_changes_nothing(tmp_path: Path) -> None:
    data, config = worker_data(tmp_path)
    wg = FakeWireGuard(config)
    assert MeshNode(data, config=config, run=wg).apply(MeshView()) == []
    assert wg.calls == [] and not config.exists()


async def test_the_worker_loop_syncs_the_network(server: LiveServer, tmp_path: Path) -> None:
    host = HostStandIn()
    _, token = await create_host(server.services.db, "apps-1", host.public_key, ["apps"],
                                 wg_public_key=key32("apps-1"))  # fmt: skip
    data, config = worker_data(tmp_path)
    wg = FakeWireGuard(config)
    client = HostClient(server.url, token, host, host.key, slots=1,  # type: ignore[arg-type]
                        mesh=MeshNode(data, config=config, run=wg))  # fmt: skip
    stop = asyncio.Event()
    serving = asyncio.create_task(client.serve(stop))
    try:
        async with asyncio.timeout(20):
            while not wg.up:
                await asyncio.sleep(0.05)
    finally:
        stop.set()
        await asyncio.wait_for(serving, 10)
    assert client.mesh_revision > 0 and config.is_file()


# -- the route -----------------------------------------------------------------------------


async def test_the_mesh_route_refuses_without_a_token_and_answers_only_the_callers_view(
    server: LiveServer,
) -> None:
    db = server.services.db
    async with httpx.AsyncClient(base_url=server.url, timeout=30) as anyone:
        assert (await anyone.get("/api/host/mesh")).status_code == 401
        bad = {"Authorization": "Bearer fhw_0000000000000000_nope"}
        assert (await anyone.get("/api/host/mesh", headers=bad)).status_code == 401
    apps, apps_token = await node(db, "apps-1", ["apps"], "198.51.100.4:51820")
    dbn, db_token = await node(db, "db-1", ["db"])
    _, storage_token = await node(db, "store-1", ["storage"])
    _, old_token = await node(db, "old", ["apps"], wg=False)
    views: dict[str, dict[str, object]] = {}
    for name, token in (("apps", apps_token), ("db", db_token), ("storage", storage_token),
                        ("old", old_token), ("db-again", db_token)):  # fmt: skip
        async with mesh_client(server, token) as http:
            reply = await http.get("/api/host/mesh")
            assert reply.status_code == 200, reply.text
            views[name] = reply.json()
            assert views[name]["revision"] == await revision(http)
    apps_ip, db_ip = (await stored(db, apps.id)).mesh_ip, (await stored(db, dbn.id)).mesh_ip
    assert views["apps"]["address"] == apps_ip and views["db"]["address"] == db_ip
    assert views["db-again"] == views["db"]  # the address stays
    assert labels(views["apps"]) == ["forge-web", dbn.id]  # never itself, never storage
    assert labels(views["db"]) == ["forge-web", apps.id]
    assert labels(views["storage"]) == ["forge-web"] and views["storage"]["rules"] == []
    assert views["db"]["rules"] == [{"source": apps_ip, "protocol": "tcp", "port_from": 5432,
                                     "port_to": 5532}]  # fmt: skip
    assert views["old"] == {"revision": 0, "address": None, "peers": [], "rules": []}
    to_apps = [p for p in views["db"]["peers"] if p["label"] == apps.id]  # type: ignore[union-attr]
    assert to_apps[0]["endpoint"] == "198.51.100.4:51820"
    assert to_apps[0]["public_key"] == key32("apps-1")
    private, public = forge_web_keys(server.services.vault)
    assert views["apps"]["peers"][0]["public_key"] == public  # type: ignore[index]
    assert private not in json.dumps(views)
    async with server.services.db.session() as session:
        audited = [f"{e.action} {e.detail}" for e in await session.scalars(select(AuditEntry))]
    assert all(private not in entry for entry in audited)


# -- the installers --------------------------------------------------------------------------


def test_the_installers_open_wireguard_and_the_worker_may_write_its_config() -> None:
    node_sh = (WEB / "packages" / "server" / "src" / "forge_web" / "hosting" / "install" /
               "node.sh.tmpl").read_text(encoding="utf-8")  # fmt: skip
    install_sh = (WEB / "deploy" / "install.sh").read_text(encoding="utf-8")
    unit = (WEB / "deploy" / "host" / "forge-host-worker.service").read_text(encoding="utf-8")
    for script in (node_sh, install_sh):
        assert "ufw allow 51820/udp" in script and "Status: active" in script
    assert "ufw allow in on forge0" in node_sh
    assert "-/etc/wireguard" in unit and "install -d -m 0700 /etc/wireguard" in node_sh
    guide = (WEB / "docs" / "EINRICHTUNG.md").read_text(encoding="utf-8")
    assert "forge-web mesh config" in guide and "51820" in guide
