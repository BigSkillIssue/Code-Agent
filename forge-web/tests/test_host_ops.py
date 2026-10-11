"""Backups of hosted apps (W29a): the host encrypts the database and the files before anything
leaves it, a changed, cut-off or wrongly keyed backup does not open, and the restore drill reads
a backup back into a throwaway database. Forge Web backs up every live app on schedule, keeps
only the encrypted file (the newest few) and drills the newest one.

Metering (W29b): what an app uses adds up over its containers and its own volumes; hosts speak
only for their own apps; an alert is mailed once per incident and again only after it ended."""

import asyncio
import base64
import os
import tarfile
import time
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from sqlalchemy import select

from docker_standin import FakeDocker, job_of, plan, release_job, runner_for, tarball
from forge_hostworker.backup import CHUNK, BackupError, decrypt_file, encrypt_file, outgoing
from forge_hostworker.wire import AppUsage, HostJob, HostUsage
from forge_web.db.hosting_models import (
    AppBackup,
    AppBackupKey,
    AppUsageSample,
    HostedApp,
    HostServer,
)
from forge_web.db.models import User
from forge_web.hosting import hosts as host_queue
from forge_web.hosting.alerts import Alerts, store_usage, totals
from forge_web.hosting.hosts import HostQueue
from forge_web.hosting.ops import Backups
from host_standin import HostStandIn, host_online
from support import LiveServer
from test_hosting_deploy import (
    app_project,
    deployed,
    host_token,
    settings_for,
    settled,
    short_polls,
    until,
)

__all__ = ["short_polls"]
KEY = os.urandom(32)
VALUES = {"BACKUP_KEY": base64.b64encode(KEY).decode()}


# On the host --------------------------------------------------------------------------------


@pytest.mark.parametrize("size", [0, 10, CHUNK, 2 * CHUNK, 2 * CHUNK + 7])
def test_encryption_round_trips_in_chunks(tmp_path: Path, size: int) -> None:
    plain, sealed, back = tmp_path / "plain", tmp_path / "sealed", tmp_path / "back"
    data = os.urandom(size)
    plain.write_bytes(data)
    encrypt_file(KEY, plain, sealed)
    decrypt_file(KEY, sealed, back)
    assert (back.read_bytes() == data and data[:64] not in sealed.read_bytes()[12:]) or size == 0


def test_a_changed_cut_off_or_wrongly_keyed_backup_does_not_open(tmp_path: Path) -> None:
    plain, sealed = tmp_path / "plain", tmp_path / "sealed"
    plain.write_bytes(os.urandom(2 * CHUNK + 100))
    encrypt_file(KEY, plain, sealed)
    whole = sealed.read_bytes()
    chunk = CHUNK + 16
    variants = {
        "wrong key": (os.urandom(32), whole),
        "cut off": (KEY, whole[: 12 + 2 * chunk]),  # the last chunk is missing
        "changed": (KEY, whole[:500] + bytes([whole[500] ^ 1]) + whole[501:]),
        "reordered": (
            KEY,
            whole[:12]
            + whole[12 + chunk : 12 + 2 * chunk]
            + whole[12 : 12 + chunk]
            + whole[12 + 2 * chunk :],
        ),
        "not a backup": (KEY, b"PK\x03\x04" + whole[4:]),
    }
    for name, (key, data) in variants.items():
        broken = tmp_path / name.replace(" ", "-")
        broken.write_bytes(data)
        with pytest.raises(BackupError):
            decrypt_file(key, broken, tmp_path / "out")


async def test_a_backup_is_encrypted_before_it_leaves_the_host(tmp_path: Path) -> None:
    docker = FakeDocker()
    runner = runner_for(tmp_path, docker)
    assert (await runner.run(release_job(storage_gb=1), tarball(tmp_path / "s.tgz"), {})).ok
    job = job_of("archive")
    result = await runner.run(job, None, VALUES)
    assert result.ok and result.checks[0].name == "archive", result
    sealed = outgoing(runner.data_dir, job.id)
    data = sealed.read_bytes()
    assert data.startswith(b"FBK1") and b"PGDMP" not in data and b"JPEG" not in data
    plain = tmp_path / "plain.tar"
    decrypt_file(KEY, sealed, plain)
    with tarfile.open(plain) as tar:
        assert sorted(tar.getnames()) == ["db.dump", "files.tar"]
        dump = tar.extractfile("db.dump")
        assert dump is not None and dump.read().startswith(b"PGDMP")
    assert ["exec", "forge-shop-staging-db", "pg_dump", "-U", "app", "-d", "app", "-Fc"] in (
        docker.calls)  # fmt: skip
    packed = next(c for c in docker.runs() if "tar" in c and "-cf" in c)
    assert "forge-shop-staging-files:/data:ro" in packed and "--network" in packed
    assert not any((runner.data_dir / "archives").glob("**/*.dump"))  # nothing readable stays
    without = await runner.run(job_of("archive"), None, {})
    assert not without.ok and "backup key" in without.error


async def test_the_restore_drill_reads_a_backup_back(tmp_path: Path) -> None:
    docker = FakeDocker()
    runner = runner_for(tmp_path, docker)
    assert (await runner.run(release_job(storage_gb=1), tarball(tmp_path / "s.tgz"), {})).ok
    archive = job_of("archive")
    assert (await runner.run(archive, None, VALUES)).ok
    source = tmp_path / "backup.fbk"
    outgoing(runner.data_dir, archive.id).replace(source)
    drilled = await runner.run(job_of("restore_drill"), source, VALUES)
    assert drilled.ok and drilled.checks[0].name == "restore", drilled
    assert "1 entries of files" in drilled.checks[0].detail
    [network] = [c[-1] for c in docker.calls if c[:3] == ["network", "create", "--internal"]]
    assert network.startswith("forge-drill-")
    [database] = [c for c in docker.runs() if f"{network}-db" in c]
    assert any(a.endswith(":/restore/db.dump:ro") for a in database)
    assert database[database.index("/var/lib/postgresql/data") - 1] == "--tmpfs"
    restore = next(
        c for c in docker.calls if c[:2] == ["exec", f"{network}-db"] and "pg_restore" in c
    )
    assert restore[-1] == "/restore/db.dump"
    assert ["rm", "-f", f"{network}-db"] in docker.calls and [
        "network",
        "rm",
        network,
    ] in docker.calls
    assert not (runner.data_dir / "drills").exists() or not any(
        (runner.data_dir / "drills").iterdir()
    )
    wrong = {"BACKUP_KEY": base64.b64encode(os.urandom(32)).decode()}
    refused = await runner.run(job_of("restore_drill"), source, wrong)
    assert not refused.ok and "does not open" in refused.error


# On the server ------------------------------------------------------------------------------


@pytest.fixture
def server(tmp_path: Path) -> Iterator[LiveServer]:
    with LiveServer(settings_for(tmp_path / "data")) as live:
        yield live


@pytest.fixture
async def client(server: LiveServer) -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(base_url=server.url, headers=server.headers(), timeout=60) as c:
        yield c


async def newest(client: httpx.AsyncClient, field: str) -> dict[str, Any] | None:
    """The staging backup of the one app, once `field` is set."""
    [app] = (await client.get("/api/admin/hosting/apps")).json()
    found = app["backups"].get("staging")
    return found if found and found[field] else None


async def test_an_admin_backs_up_and_drills_an_app(
    server: LiveServer, client: httpx.AsyncClient
) -> None:
    host = HostStandIn()
    project_id, _ = await app_project(server, client, server.services.dev_user_id)
    async with host_online(server.url, await host_token(server, host), host):
        deploy_id = await deployed(client, project_id, "staging")
        await until(lambda: settled(client, project_id, deploy_id))
        body = {"environment": "staging"}
        url = f"/api/admin/hosting/apps/{project_id}"
        assert (await client.post(f"{url}/backup", json=body)).json() == {"started": True}
        made = await until(lambda: newest(client, "at"))
        assert (await client.post(f"{url}/drill", json=body)).json() == {"started": True}
        drilled = await until(lambda: newest(client, "drill_at"))
    archive, drill = host.last("archive"), host.last("restore_drill")
    stored = server.services.backups.path(project_id, "staging", archive.id)
    assert (
        stored.read_bytes() == host.archives[archive.id] and made["size"] == stored.stat().st_size
    )
    assert host.drilled[drill.id] == stored.read_bytes()  # the drill got exactly that backup
    key = host.secrets[archive.id]["BACKUP_KEY"]
    assert len(base64.b64decode(key)) == 32 and host.secrets[drill.id]["BACKUP_KEY"] == key
    assert drilled["drill_ok"] and "3 tables" in drilled["drill_detail"]
    async with server.services.db.session() as session:
        kept = await session.get(AppBackupKey, project_id)
    assert kept is not None and key not in kept.key  # kept encrypted
    assert not any(host.data_dir.glob("outgoing/*"))  # uploaded, then gone from the host


async def test_the_schedule_backs_up_drills_and_keeps_the_newest(
    server: LiveServer, client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(host_queue, "RECHECK_S", 0.2)
    host = HostStandIn()
    project_id, _ = await app_project(server, client, server.services.dev_user_id)
    services = server.services
    # A schedule of its own on this test's loop, on the server's database and files.
    backups = Backups(services.db, services.vault, services.settings,
                      HostQueue(services.db, services.settings.data_dir, 600))  # fmt: skip
    async with host_online(server.url, await host_token(server, host), host):
        assert await backups.tick() == []  # nothing is live yet
        deploy_id = await deployed(client, project_id, "staging")
        await until(lambda: settled(client, project_id, deploy_id))
        first = await backups.tick()
        assert [await task for task in first] == [""]  # a backup
        second = await backups.tick()
        assert [await task for task in second] == [""]  # then its drill
        assert await backups.tick() == []  # nothing is due now
        services.settings.hosting.keep_backups = 1
        assert await backups.backup(project_id, "staging") == ""
    assert host.kinds()[-3:] == ["archive", "restore_drill", "archive"]
    async with services.db.session() as session:
        rows = list(await session.scalars(select(AppBackup)))
    assert len(rows) == 1 and rows[0].id == host.last("archive").id
    folder = services.settings.data_dir / "hosting" / "backups" / project_id / "staging"
    assert [p.stem for p in folder.iterdir()] == [rows[0].id]
    await asyncio.sleep(0)


# Metering and alerts ------------------------------------------------------------------------


async def test_metering_adds_up_per_app(tmp_path: Path) -> None:
    docker = FakeDocker()
    runner = runner_for(tmp_path, docker)
    assert (await runner.run(release_job(storage_gb=1), tarball(tmp_path / "s.tgz"), {})).ok
    blog = HostJob(id="b" * 32, kind="release", app="blog", environment="production",
                   plan=plan(app="blog", environment="production", database=False))  # fmt: skip
    assert (await runner.run(blog, tarball(tmp_path / "b.tgz"), {})).ok
    docker.containers["forge-blog-production-web-r1"]["running"] = False
    docker.stats = {
        "forge-shop-staging-api-r1": {"CPUPerc": "12.5%", "MemUsage": "100MiB / 512MiB",
                                      "NetIO": "1MB / 2MB"},
        "forge-shop-staging-web-r1": {"CPUPerc": "2.5%", "MemUsage": "20MiB / 256MiB",
                                      "NetIO": "500kB / 1.5MB"},
        "forge-shop-staging-db": {"CPUPerc": "5.00%", "MemUsage": "1GiB / 2GiB",
                                  "NetIO": "0B / 0B"},
        "forge-blog-production-api-r1": {"CPUPerc": "1%", "MemUsage": "10MB / 512MiB",
                                         "NetIO": "3kB / 4kB"},
    }  # fmt: skip
    docker.volumes |= {"forge-shop-staging-db-data", "forge-shop-staging-x-staging-files"}
    docker.volume_sizes = {"forge-shop-staging-files": "1.5GB", "forge-shop-staging-db-data":
                           "200MB", "forge-shop-staging-x-staging-files": "9GB"}  # fmt: skip
    (runner.folder("shop", "staging") / "upload.bin").write_bytes(bytes(1_000_000))
    report = await runner.usage()
    apps = {(a.app, a.environment): a for a in report.apps}
    assert sorted(apps) == [("blog", "production"), ("shop", "staging")]
    shop, blog_usage = apps[("shop", "staging")], apps[("blog", "production")]
    assert (shop.containers, shop.running, blog_usage.containers, blog_usage.running) == (
        3,
        3,
        2,
        1,
    )
    assert shop.cpu_percent == pytest.approx(20.0) and blog_usage.cpu_percent == pytest.approx(1.0)
    assert shop.memory_mb == pytest.approx(1144 * 1.048576)
    assert (shop.net_rx_mb, shop.net_tx_mb) == (pytest.approx(1.5), pytest.approx(3.5))
    assert 1701 <= shop.disk_mb < 1702  # its two volumes and its folder, not the other app's
    assert blog_usage.disk_mb < 1 and report.disk_free_gb > 0
    [stats] = [c for c in docker.calls if c[0] == "stats"]
    assert "forge-blog-production-web-r1" not in stats  # a stopped container uses nothing


def sample(at: float, cpu: float, traffic: float, memory: float = 0, disk: float = 0) -> Any:
    return AppUsageSample(id=f"s{at}", project_id="p", environment="staging", at=at,
                          containers=1, running=1, cpu_percent=cpu, memory_mb=memory,
                          net_rx_mb=traffic / 2, net_tx_mb=traffic / 2, disk_mb=disk)  # fmt: skip


def test_samples_add_up_to_cpu_hours_traffic_and_peaks() -> None:
    samples = [sample(3600, 100, 5, 80, 900), sample(0, 50, 10, 100, 700),
               sample(1800, 50, 30, 50, 800)]  # fmt: skip
    found = totals(samples)
    assert found["cpu_hours"] == pytest.approx(0.5)  # half a core for an hour
    assert found["traffic_mb"] == pytest.approx(25)  # 20, then 5 after a restart
    assert (found["memory_mb_max"], found["disk_mb_max"], found["samples"]) == (100, 900, 3)
    assert totals([])["cpu_hours"] == 0


async def hosted_on(server: LiveServer, project_id: str) -> tuple[HostedApp, HostServer]:
    async with server.services.db.session() as session:
        hosted = await session.get(HostedApp, project_id)
        assert hosted is not None
        host = await session.get(HostServer, hosted.host_id)
        assert host is not None
        return hosted, host


async def test_a_host_reports_only_for_its_own_apps(
    server: LiveServer, client: httpx.AsyncClient
) -> None:
    host = HostStandIn()
    project_id, _ = await app_project(server, client, server.services.dev_user_id)
    token = await host_token(server, host)
    async with host_online(server.url, token, host):
        deploy_id = await deployed(client, project_id, "staging")
        await until(lambda: settled(client, project_id, deploy_id))
    hosted, _ = await hosted_on(server, project_id)
    report = HostUsage(disk_free_gb=42, apps=[
        AppUsage(app=hosted.app, environment="staging", containers=3, running=3, cpu_percent=20,
                 memory_mb=300, net_rx_mb=1, net_tx_mb=2, disk_mb=1500),
        AppUsage(app="other", environment="staging", containers=1, running=1, memory_mb=9),
    ])  # fmt: skip
    as_host = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    for _ in range(2):
        sent = await client.post("/api/host/metrics", content=report.model_dump_json(),
                                 headers=as_host)  # fmt: skip
        assert sent.json() == {"kept": 1}, sent.text
    used = (await client.get(f"/api/admin/hosting/apps/{project_id}/usage")).json()
    assert list(used) == ["staging"] and used["staging"]["samples"] == 2
    assert used["staging"]["disk_mb_max"] == 1500 and used["staging"]["memory_mb_max"] == 300
    async with server.services.db.session() as session:
        rows = list(await session.scalars(select(AppUsageSample)))
    assert {r.project_id for r in rows} == {project_id}  # nothing kept for the other app
    assert (await hosted_on(server, project_id))[1].disk_free_gb == 42
    forged = as_host | {"Authorization": "Bearer fhw_forged"}
    refused = await client.post("/api/host/metrics", content=report.model_dump_json(),
                                headers=forged)  # fmt: skip
    assert refused.status_code == 401


async def test_an_alert_goes_out_once_per_incident(
    server: LiveServer, client: httpx.AsyncClient
) -> None:
    host = HostStandIn()
    project_id, _ = await app_project(server, client, server.services.dev_user_id)
    mails: list[tuple[str, str, str]] = []

    async def mailer(to: str, subject: str, text: str) -> None:
        mails.append((to, subject, text))

    services = server.services
    alerts = Alerts(services.db, services.settings, mailer)
    async with host_online(server.url, await host_token(server, host), host):
        deploy_id = await deployed(client, project_id, "staging")
        await until(lambda: settled(client, project_id, deploy_id))
        hosted, host_row = await hosted_on(server, project_id)
        async with services.db.session() as session:
            admins = sorted(await session.scalars(select(User.email).where(User.role == "admin")))
        where, broken = f"{project_id}:staging", HostUsage(disk_free_gb=3, apps=[
            AppUsage(app=hosted.app, environment="staging", containers=3, running=2,
                     memory_mb=1e6)])  # fmt: skip
        await store_usage(services.db, host_row, broken)
        first = await alerts.check()
        assert sorted(first) == [f"disk:{host_row.id}", f"down:{where}", f"memory:{where}"]
        assert await alerts.check() == []  # the same incidents: nothing new is mailed
        assert len(mails) == 3 * len(admins) and sorted({m[0] for m in mails}) == admins
        assert any("2 von 3 Containern" in text for _, _, text in mails)
        healed = HostUsage(disk_free_gb=80, apps=[
            AppUsage(app=hosted.app, environment="staging", containers=3, running=3,
                     memory_mb=10)])  # fmt: skip
        await store_usage(services.db, host_row, healed)
        assert await alerts.check() == []
        shown = (await client.get("/api/admin/hosting/incidents")).json()
        assert len(shown) == 3 and all(row["closed_at"] > 0 for row in shown)  # they ended
        await store_usage(services.db, host_row, broken)
        assert sorted(await alerts.check()) == first  # back again: new incidents, new mails
        assert len(mails) == 6 * len(admins)
        await store_usage(services.db, host_row, healed)
        every = services.settings.hosting.backup_every_h * 3600
        async with services.db.session() as session, session.begin():
            session.add(AppBackup(id="old", project_id=project_id, environment="staging", size=1,
                                  created_at=time.time() - 3 * every, drill_at=time.time(),
                                  drill_ok=False, drill_detail="pg_restore: error"))  # fmt: skip
        assert sorted(await alerts.check()) == [f"backup:{where}", f"drill:{where}"]
    assert any("pg_restore: error" in text for _, _, text in mails[-len(admins) * 2 :])
