"""Server roles and join codes (W63): an admin makes a one-time code `fjn_…` with a name and
roles; a new server trades it once, within 24 hours, for its `fhw_…` token, with its X25519 and
WireGuard keys. Codes cannot be guessed and every refusal looks the same; hosts from before are
`apps` hosts, apps are placed only on `apps` hosts, and the worker runs only its roles' loops."""

import asyncio
import base64
import contextlib
import hashlib
import json
import sys
import time
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
import sqlalchemy as sa
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
from sqlalchemy import select

from forge_hostworker import cli
from forge_hostworker.client import HostClient
from forge_hostworker.wire import HostJob, HostUsage, JobResult, PollAnswer, public_key
from forge_web.auth.ratelimit import RateLimiter
from forge_web.db.engine import Database
from forge_web.db.hosting_models import HostJobRow, HostJoinCode, HostServer
from forge_web.db.models import AuditEntry
from forge_web.hosting import deploy_api, hosts
from forge_web.hosting.hosts import create_host, pick_host
from forge_web.hosting.join import JOIN_TTL_S, create_join_code, valid_roles
from host_standin import HostStandIn
from support import LiveServer, person
from test_hosting_deploy import settings_for
from test_one_chat import migrate

ADMIN = "/api/admin/hosting"
REFUSED = 403
POSIX = sys.platform != "win32"


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
async def admin(server: LiveServer) -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(base_url=server.url, headers=server.headers(), timeout=60) as c:
        yield c


def key32(seed: str) -> str:
    """A base64 key of 32 bytes (as `wg pubkey` and `keygen` print them)."""
    return base64.b64encode(hashlib.sha256(seed.encode()).digest()).decode()


def keys(code: str, endpoint: str | None = None) -> dict[str, Any]:
    """A join request with fresh keys."""
    body: dict[str, Any] = {"code": code, "public_key": public_key(X25519PrivateKey.generate()),
                            "wg_public_key": key32(code)}  # fmt: skip
    if endpoint is not None:
        body["endpoint"] = endpoint
    return body


async def join_code(admin: httpx.AsyncClient, roles: list[str], name: str = "node-1") -> str:
    made = await admin.post(f"{ADMIN}/join-codes", json={"name": name, "roles": roles})
    assert made.status_code == 200, made.text
    return str(made.json()["code"])


async def host_row(server: LiveServer, host_id: str) -> HostServer:
    async with server.services.db.session() as session:
        row = await session.get(HostServer, host_id)
    assert row is not None
    return row


async def joining(server: LiveServer, body: dict[str, Any]) -> httpx.Response:
    async with httpx.AsyncClient(base_url=server.url, timeout=30) as anyone:
        return await anyone.post("/api/host/join", json=body)


async def polled(server: LiveServer, token: str) -> httpx.Response:
    async with httpx.AsyncClient(base_url=server.url, timeout=30) as host:
        return await host.post("/api/host/poll", json={"free_slots": 1},
                               headers={"Authorization": f"Bearer {token}"})  # fmt: skip


async def test_a_code_works_once_and_its_token_is_answered_once(
    server: LiveServer, admin: httpx.AsyncClient
) -> None:
    made = await admin.post(f"{ADMIN}/join-codes",
                            json={"name": "storage-1", "roles": ["storage", "apps"]})  # fmt: skip
    assert made.status_code == 200, made.text
    code = made.json()["code"]
    assert code.startswith("fjn_") and made.json()["roles"] == ["apps", "storage"]
    assert made.json()["expires_at"] - time.time() == pytest.approx(JOIN_TTL_S, abs=60)
    command = made.json()["command"]  # the test server's address is not https: no command
    assert command is None or (f"--code {code}" in command and "--roles apps,storage" in command)
    body = keys(code, "203.0.113.7:51820")
    joined = await joining(server, body)
    assert joined.status_code == 200, joined.text
    answer = joined.json()
    assert answer["token"].startswith("fhw_") and answer["roles"] == ["apps", "storage"]
    assert answer["name"] == "storage-1"
    row = await host_row(server, answer["host_id"])
    assert (row.public_key, row.wg_public_key) == (body["public_key"], body["wg_public_key"])
    assert row.endpoint == "203.0.113.7:51820" and row.mesh_ip is None
    assert json.loads(row.roles) == ["apps", "storage"]
    poll = await polled(server, answer["token"])
    assert poll.status_code == 200 and poll.json()["roles"] == ["apps", "storage"]
    again = await joining(server, keys(code))
    assert again.status_code == REFUSED
    async with server.services.db.session() as session:
        actions = list(await session.scalars(select(AuditEntry.action)))
        used = await session.scalar(select(HostJoinCode))
    assert {"hosting.join_code_made", "hosting.host_joined"} <= set(actions)
    assert used is not None and used.used_at > 0 and used.host_id == answer["host_id"]
    hosts_now = (await admin.get(f"{ADMIN}/hosts")).json()
    assert [h["roles"] for h in hosts_now] == [["apps", "storage"]]
    assert all("token" not in h for h in hosts_now)


async def test_an_expired_code_is_refused(server: LiveServer) -> None:
    db = server.services.db
    row, code = await create_join_code(db, "late", ["apps"], created_by="admin")
    assert row.expires_at - row.created_at == JOIN_TTL_S
    async with db.session() as session, session.begin():
        stored = await session.get(HostJoinCode, row.id)
        assert stored is not None
        stored.expires_at = time.time() - 1
    late = await joining(server, keys(code))
    assert late.status_code == REFUSED
    async with db.session() as session:
        assert list(await session.scalars(select(HostServer))) == []


async def test_wrong_and_garbled_codes_are_refused_alike_and_spend_nothing(
    server: LiveServer, admin: httpx.AsyncClient
) -> None:
    code = await join_code(admin, ["apps"])
    _, code_id, secret = code.split("_", 2)
    wrong = ["", "fjn", "fjn_", "fjn__", f"fjn_{code_id}", f"fjn_{code_id}_",
             f"fjn_{code_id}_{secret[:-1]}x", f"fjn_nosuchcode_{secret}", f"fhw_{code_id}_{secret}",
             f"FJN_{code_id}_{secret}", f" {code}x", "fjn_" + "a" * 150]  # fmt: skip
    details = set()
    async with httpx.AsyncClient(base_url=server.url) as anyone:
        for guess in wrong:
            refused = await anyone.post("/api/host/join", json=keys(guess))
            assert refused.status_code == REFUSED, (guess, refused.text)
            details.add(refused.json()["detail"])
        assert len(details) == 1  # nothing tells a used code from a wrong one
        assert (await anyone.post("/api/host/join", json=keys(code))).status_code == 200


async def test_codes_cannot_be_guessed(server: LiveServer) -> None:
    db = server.services.db
    made = [await create_join_code(db, f"n{i}", ["apps"], created_by="admin") for i in range(5)]
    codes = [code for _, code in made]
    assert len(set(codes)) == 5
    for row, code in made:
        _, code_id, secret = code.split("_", 2)
        assert code_id == row.id and len(secret) >= 43  # 256 bits from `secrets`
        assert row.code_hash == hashlib.sha256(secret.encode()).hexdigest()
        assert secret not in json.dumps({c.name: str(getattr(row, c.key)) for c in
                                         HostJoinCode.__table__.columns})  # fmt: skip


async def test_a_join_attempt_is_rate_limited(server: LiveServer) -> None:
    server.services.limits.joins_by_ip = RateLimiter(2, 3600)
    async with httpx.AsyncClient(base_url=server.url) as anyone:
        tries = [await anyone.post("/api/host/join", json=keys("fjn_a_b")) for _ in range(3)]
    assert [t.status_code for t in tries] == [REFUSED, REFUSED, 429]


async def test_roles_are_validated(server: LiveServer, admin: httpx.AsyncClient) -> None:
    for roles in ([], ["web"], ["apps", "root"], "apps"):
        made = await admin.post(f"{ADMIN}/join-codes", json={"name": "n", "roles": roles})
        assert made.status_code == 422, (roles, made.text)
    assert valid_roles(["storage", "ai", "storage", "db", "apps"]) == [
        "apps",
        "db",
        "ai",
        "storage",
    ]
    with pytest.raises(ValueError):
        valid_roles([])
    with pytest.raises(ValueError):
        await create_join_code(server.services.db, "n", ["mail"], created_by="admin")
    with pytest.raises(ValueError):
        await create_host(server.services.db, "h", HostStandIn().public_key, ["gpu"])
    member = await person(server, "mallory")
    refused = await member.web.post(f"{ADMIN}/join-codes", {"name": "n", "roles": ["apps"]})
    assert refused.status_code == 403


async def test_keys_and_endpoint_are_validated_before_the_code_is_spent(
    server: LiveServer, admin: httpx.AsyncClient
) -> None:
    code = await join_code(admin, ["db"])
    bad = [
        {**keys(code), "wg_public_key": "not base64!"},
        {**keys(code), "wg_public_key": base64.b64encode(b"x" * 31).decode()},
        {**keys(code), "public_key": base64.b64encode(b"x" * 33).decode()},
        keys(code, "203.0.113.7"),
        keys(code, "203.0.113.7:0"),
        keys(code, "203.0.113.7:70000"),
        keys(code, "[not-an-ip]:51820"),
        keys(code, "bad host:51820"),
    ]
    async with httpx.AsyncClient(base_url=server.url) as anyone:
        for body in bad:
            refused = await anyone.post("/api/host/join", json=body)
            assert refused.status_code == 422, (body, refused.text)
        joined = await anyone.post("/api/host/join", json=keys(code, "[2001:db8::1]:51820"))
    assert joined.status_code == 200, joined.text
    assert (await host_row(server, joined.json()["host_id"])).endpoint == "[2001:db8::1]:51820"


async def test_hosts_added_the_old_way_are_apps_hosts(
    server: LiveServer, admin: httpx.AsyncClient
) -> None:
    old_way = {"name": "old", "public_key": HostStandIn().public_key}
    added = await admin.post(f"{ADMIN}/hosts", json=old_way)
    assert added.status_code == 200, added.text
    assert added.json()["roles"] == ["apps"] and added.json()["token"].startswith("fhw_")
    row = await host_row(server, added.json()["id"])
    assert json.loads(row.roles) == ["apps"] and row.wg_public_key is None
    assert (await polled(server, added.json()["token"])).json()["roles"] == ["apps"]


def test_hosts_from_before_the_migration_stay_apps_hosts(tmp_path: Path) -> None:
    url = f"sqlite:///{tmp_path / 'old.db'}"
    migrate(url, "0042")
    engine = sa.create_engine(url)
    with engine.begin() as db:
        db.execute(sa.text("INSERT INTO host_servers (id, name, token_hash, public_key, enabled, "
                           "created_at, last_seen, version, disk_free_gb) VALUES "
                           "('h1', 'old', 'x', 'k', true, 0, 0, '', 0)"))  # fmt: skip
    migrate(url, "head")
    with engine.connect() as db:
        row = db.execute(sa.text("SELECT roles, mesh_ip, wg_public_key, endpoint, gpu, models "
                                 "FROM host_servers")).one()  # fmt: skip
        assert json.loads(row[0]) == ["apps"] and tuple(row[1:]) == (None,) * 5
        assert db.execute(sa.text("SELECT count(*) FROM host_join_codes")).scalar() == 0
    engine.dispose()


async def test_apps_are_placed_only_on_apps_hosts(tmp_path: Path) -> None:
    db = Database(f"sqlite+aiosqlite:///{tmp_path / 'p.db'}")
    await db.migrate()
    key = HostStandIn().public_key
    await create_host(db, "storage", key, ["storage"])
    await create_host(db, "models", key, ["ai", "db"])
    assert await pick_host(db) is None
    off, _ = await create_host(db, "off", key, ["apps"])
    async with db.session() as session, session.begin():
        stored = await session.get(HostServer, off.id)
        assert stored is not None
        stored.enabled = False
    assert await pick_host(db) is None
    both, _ = await create_host(db, "both", key, ["storage", "apps"])
    picked = await pick_host(db)
    assert picked is not None and picked.id == both.id
    await db.close()


def test_the_poll_answer_names_the_roles_and_an_old_answer_means_apps() -> None:
    assert PollAnswer.model_validate_json('{"job": null}').roles == ["apps"]
    newer = PollAnswer.model_validate_json('{"job": null, "roles": ["storage", "future"]}')
    assert newer.roles == ["storage"]  # a role this worker does not know yet is left out


class FakeWg:
    """`wg genkey` and `wg pubkey` without WireGuard installed."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def __call__(self, argv: list[str], stdin: str) -> str:
        self.calls.append(argv)
        if argv == ["wg", "genkey"]:
            return key32("private") + "\n"
        assert argv == ["wg", "pubkey"] and stdin.strip() == key32("private")
        return key32("public") + "\n"


def mode(path: Path) -> int:
    return path.stat().st_mode & 0o777


async def test_the_worker_joins_against_the_real_router(
    server: LiveServer, admin: httpx.AsyncClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    code = await join_code(admin, ["apps", "ai"], name="gpu-1")
    data = tmp_path / "node"
    wg = FakeWg()
    monkeypatch.setattr(cli, "wg_command", wg)
    args = ["join", "--server", server.url, "--code", code, "--data", str(data),
            "--endpoint", "198.51.100.4:51820"]  # fmt: skip
    assert await asyncio.to_thread(cli.main, args) == 0
    token = (data / "token").read_text(encoding="utf-8").strip()
    assert token.startswith("fhw_")
    assert (data / "wg.key").read_text(encoding="utf-8").strip() == key32("private")
    if POSIX:
        assert mode(data / "token") == mode(data / "wg.key") == mode(data / "host.key") == 0o600
    assert wg.calls == [["wg", "genkey"], ["wg", "pubkey"]]
    async with server.services.db.session() as session:
        row = await session.scalar(select(HostServer))
    assert row is not None and row.name == "gpu-1" and json.loads(row.roles) == ["apps", "ai"]
    assert row.public_key == public_key(cli.load_key(data / "host.key"))
    assert row.wg_public_key == key32("public") and row.endpoint == "198.51.100.4:51820"
    assert (await polled(server, token)).json()["roles"] == ["apps", "ai"]
    other = await join_code(admin, ["apps"])
    again = ["join", "--server", server.url, "--code", other, "--data", str(data)]
    assert await asyncio.to_thread(cli.main, again) == 2  # joined already: the code is kept
    assert (await joining(server, keys(other))).status_code == 200


async def test_a_refused_join_writes_no_token(
    server: LiveServer, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    data = tmp_path / "node"
    assert await cli.join(server.url, "fjn_nosuch_secret", data, wg=FakeWg()) == 2
    assert not (data / "token").exists()
    assert "join code is not valid" in capsys.readouterr().err
    assert await cli.join(server.url, "fhw_not_a_join_code", data, wg=FakeWg()) == 2


class CountingHost(HostStandIn):
    """A stand-in that counts its usage reports."""

    def __init__(self) -> None:
        super().__init__()
        self.reports = 0

    async def usage(self) -> HostUsage:
        self.reports += 1
        return self.metered


@contextlib.asynccontextmanager
async def online(server: LiveServer, token: str, host: CountingHost) -> AsyncIterator[HostClient]:
    client = HostClient(server.url, token, host, host.key, slots=1)  # type: ignore[arg-type]
    client.usage_every_s = 0.1
    stop = asyncio.Event()
    serving = asyncio.create_task(client.serve(stop))
    try:
        yield client
    finally:
        stop.set()
        await asyncio.wait_for(serving, 10)


async def reported(server: LiveServer, job_id: str) -> JobResult:
    """The job's result once its host reported it."""
    while True:
        async with server.services.db.session() as session:
            row = await session.get(HostJobRow, job_id)
        if row is not None and row.status == "done":
            return JobResult.model_validate_json(row.result)
        await asyncio.sleep(0.1)


async def test_the_worker_runs_only_the_loops_of_its_roles(server: LiveServer) -> None:
    db = server.services.db
    storage = CountingHost()
    storage_host, storage_token = await create_host(db, "storage", storage.public_key, ["storage"])
    apps = CountingHost()
    _, apps_token = await create_host(db, "apps", apps.public_key)
    job = HostJob(id="a" * 32, kind="logs", app="shop", environment="staging", timeout_s=30)
    async with db.session() as session, session.begin():  # never placed so; refused anyway
        session.add(HostJobRow(id=job.id, host_id=storage_host.id, app=job.app, kind=job.kind,
                               job=job.model_dump_json(), status="queued",
                               created_at=time.time()))  # fmt: skip
    async with online(server, storage_token, storage) as client:
        result = await asyncio.wait_for(reported(server, job.id), 30)
        await asyncio.sleep(0.5)
        assert client.roles == ["storage"]
    assert storage.reports == 0 and storage.jobs == []
    assert not result.ok and "apps role" in result.error
    async with online(server, apps_token, apps) as client:
        async with asyncio.timeout(10):
            while apps.reports == 0:
                await asyncio.sleep(0.05)
        assert client.roles == ["apps"]
