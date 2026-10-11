"""Export and full deletion of a hosted app (W29c): the export holds the database and the files,
decrypted for the project's owner only; deleting an app leaves nothing of it on its host (other
apps and environments stay) and nothing on this server but the project, and the edge forgets its
certificate. A project with a hosted app is deleted only after its app."""

import asyncio
import io
import tarfile
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from sqlalchemy import func, select

from docker_standin import FakeDocker, plan, release_job, runner_for, tarball
from forge_hostworker.edge import Edge, forget_certificates
from forge_hostworker.wire import AppUsage, HostJob, HostUsage
from forge_web.db.hosting_models import (
    AbuseNotice,
    AppBackup,
    AppBackupKey,
    AppMail,
    AppOperator,
    AppSecret,
    AppUsageSample,
    Deploy,
    GoLiveApproval,
    HostedApp,
    HostJobRow,
    HostServer,
    Incident,
)
from forge_web.db.models import ProjectMember
from forge_web.hosting.alerts import store_usage
from host_standin import ARCHIVE, HostStandIn, host_online
from support import LiveServer, person
from test_host_edge import DOMAIN, LEGAL, Caddy
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
EVERY_TABLE = (AbuseNotice, AppBackup, AppBackupKey, AppMail, AppOperator, AppSecret,
               AppUsageSample, Deploy, GoLiveApproval, HostedApp)  # fmt: skip


# On the host --------------------------------------------------------------------------------


def job(kind: str, app: str, environment: str, **changes: Any) -> HostJob:
    shown = plan(app=app, environment=environment, **changes) if kind == "release" else None
    return HostJob(id=("c" if kind == "delete" else "a") * 32, kind=kind, app=app,
                   environment=environment, plan=shown)  # fmt: skip


async def test_deleting_leaves_nothing_of_the_app_on_its_host(tmp_path: Path) -> None:
    docker = FakeDocker()
    runner = runner_for(tmp_path, docker)
    for environment in ("staging", "production"):
        made = await runner.run(job("release", "shop", environment, storage_gb=1),
                                tarball(tmp_path / f"{environment}.tgz"), {})  # fmt: skip
        assert made.ok, made
    docker.volumes |= {"forge-shop-staging-db-data", "forge-shop-production-db-data"}
    changed: list[str] = []

    async def edge_reload() -> None:
        changed.append("reloaded")

    runner.on_change = edge_reload
    deleted = await runner.run(job("delete", "shop", "staging"), None, {})
    assert deleted.ok, deleted
    assert not [name for name in docker.containers if name.startswith("forge-shop-staging-")]
    assert "forge-shop-staging" not in docker.networks
    assert not {v for v in docker.volumes if v.startswith("forge-shop-staging-")}
    assert not runner.folder("shop", "staging").exists() and changed == ["reloaded"]
    # production of the same app is untouched
    assert "forge-shop-production-db" in docker.containers and "forge-shop-production" in (
        docker.networks)  # fmt: skip
    assert {"forge-shop-production-files", "forge-shop-production-db-data"} <= docker.volumes
    assert runner.state("shop", "production").live == 1
    again = await runner.run(job("delete", "shop", "staging"), None, {})
    assert again.ok  # nothing left to delete is fine


def certificate(storage: Path, host: str) -> Path:
    folder = storage / "certificates" / "acme-v02.api.letsencrypt.org-directory" / host
    folder.mkdir(parents=True)
    (folder / f"{host}.key").write_text("-----BEGIN EC PRIVATE KEY-----")
    return folder


async def test_the_edge_forgets_the_certificate_of_a_deleted_app(tmp_path: Path) -> None:
    docker, caddy, storage = FakeDocker(), Caddy(), tmp_path / "caddy"
    runner = runner_for(tmp_path, docker)
    edge = Edge(runner, docker, DOMAIN, LEGAL, certificates=storage, transport=caddy.transport())
    runner.on_change = edge.changed
    assert (await runner.run(release_job(), tarball(tmp_path / "s.tgz"), {})).ok
    shop = certificate(storage, "shop.staging.apps.example")
    other = certificate(storage, "blog.apps.example")  # an app of another host, say: not ours
    (runner.data_dir / "apps" / "blog-production").mkdir()
    custom = certificate(storage, "www.example.org")  # not under the apps domain
    await edge.refresh()
    assert shop.is_dir() and other.is_dir()  # live apps keep theirs
    assert (await runner.run(job("delete", "shop", "staging"), None, {})).ok
    assert not shop.exists() and other.is_dir() and custom.is_dir()
    assert forget_certificates(storage, DOMAIN, runner.data_dir / "apps") == []
    await edge.close()


# On the server ------------------------------------------------------------------------------


@pytest.fixture
def server(tmp_path: Path) -> Iterator[LiveServer]:
    with LiveServer(settings_for(tmp_path / "data")) as live:
        yield live


@pytest.fixture
async def client(server: LiveServer) -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(base_url=server.url, headers=server.headers(), timeout=60) as c:
        yield c


async def exports(client: httpx.AsyncClient, project_id: str) -> list[dict[str, Any]]:
    return list((await client.get(f"/api/projects/{project_id}/hosting")).json()["backups"])


async def test_the_export_holds_the_database_and_the_files(
    server: LiveServer, client: httpx.AsyncClient
) -> None:
    host = HostStandIn()
    project_id, _ = await app_project(server, client, server.services.dev_user_id)
    url = f"/api/projects/{project_id}/hosting"
    async with host_online(server.url, await host_token(server, host), host):
        refused = await client.post(f"{url}/export", json={"environment": "staging"})
        assert refused.status_code == 409  # nothing is live yet
        deploy_id = await deployed(client, project_id, "staging")
        await until(lambda: settled(client, project_id, deploy_id))
        started = await client.post(f"{url}/export", json={"environment": "staging"})
        assert started.json() == {"started": True}, started.text
        [made] = await until(lambda: exports(client, project_id))
    got = await client.get(f"{url}/backups/{made['id']}")
    assert got.status_code == 200 and got.headers["content-type"] == "application/x-tar"
    assert f'filename="{host.last("archive").app}-staging-' in got.headers["content-disposition"]
    with tarfile.open(fileobj=io.BytesIO(got.content)) as tar:
        assert sorted(tar.getnames()) == ["db.dump", "files.tar"]
        dump, files = tar.extractfile("db.dump"), tar.extractfile("files.tar")
        assert dump is not None and dump.read() == ARCHIVE["db.dump"]
        assert files is not None and files.read() == ARCHIVE["files.tar"]
    assert got.content not in host.archives[made["id"]]  # it was encrypted until now
    viewer = await person(server, "viewer")
    await member(server, project_id, viewer.id, "editor")
    assert (await viewer.web.get(f"{url}/backups/{made['id']}")).status_code == 403
    assert (await client.get(f"{url}/backups/{'0' * 16}")).status_code == 404


async def member(server: LiveServer, project_id: str, user_id: str, role: str) -> None:
    async with server.services.db.session() as session, session.begin():
        session.add(ProjectMember(project_id=project_id, user_id=user_id, role=role))


async def leftovers(server: LiveServer, project_id: str, app: str) -> dict[str, int]:
    """How many rows of the app are left, per table."""
    found = {}
    async with server.services.db.session() as session:
        for model in EVERY_TABLE:
            found[model.__name__] = await session.scalar(
                select(func.count()).select_from(model).where(model.project_id == project_id)
            )
        found["HostJobRow"] = await session.scalar(
            select(func.count()).select_from(HostJobRow).where(HostJobRow.app == app)
        )
        found["Incident"] = await session.scalar(
            select(func.count()).select_from(Incident).where(Incident.key.contains(project_id))
        )
    return {name: count or 0 for name, count in found.items() if count}


async def test_deleting_an_app_leaves_nothing_behind(
    server: LiveServer, client: httpx.AsyncClient
) -> None:
    host = HostStandIn()
    services = server.services
    project_id, _ = await app_project(server, client, services.dev_user_id)
    url = f"/api/projects/{project_id}/hosting"
    assert (
        await client.put(
            f"{url}/secrets", json={"environment": "staging", "name": "MAPS_KEY", "value": "s3cr3t"}
        )
    ).status_code == 200
    async with host_online(server.url, await host_token(server, host), host):
        deploy_id = await deployed(client, project_id, "staging")
        await until(lambda: settled(client, project_id, deploy_id))
        assert (
            await client.post(f"{url}/export", json={"environment": "staging"})
        ).status_code == 200
        await until(lambda: exports(client, project_id))
        async with services.db.session() as session:
            hosted = await session.get(HostedApp, project_id)
            row = await session.get(HostServer, hosted.host_id if hosted else "")
        assert hosted is not None and row is not None
        await store_usage(services.db, row, HostUsage(apps=[AppUsage(
            app=hosted.app, environment="staging", containers=1, running=0)]))  # fmt: skip
        await services.alerts.check()  # an incident about the app
        assert (await leftovers(server, project_id, hosted.app)).keys() >= {
            "AppBackup",
            "AppBackupKey",
            "AppSecret",
            "Deploy",
            "HostedApp",
            "Incident",
        }
        removed = await client.delete(f"/api/projects/{project_id}")
        assert removed.status_code == 409  # its app goes first
        assert (await client.delete(url)).json() == {"deleting": True}
        await until(lambda: gone(client, project_id))
    assert host.kinds()[-2:] == ["delete", "delete"]
    assert [j.environment for j in host.jobs[-2:]] == ["staging", "production"]
    assert await leftovers(server, project_id, hosted.app) == {}
    hosting_dir = services.settings.data_dir / "hosting"
    assert not (hosting_dir / "backups" / project_id).exists()
    assert not [p for p in (hosting_dir / "jobs").glob("*") if p.is_dir()]
    legal = await client.get(f"/legal/{hosted.app}/impressum")
    assert legal.status_code == 404
    assert (await client.delete(f"/api/projects/{project_id}")).status_code == 204


async def gone(client: httpx.AsyncClient, project_id: str) -> bool:
    return not (await client.get(f"/api/projects/{project_id}/hosting")).json()["app"]


async def test_a_deletion_waits_for_its_host_and_refuses_deploys_meanwhile(
    server: LiveServer, client: httpx.AsyncClient
) -> None:
    host = HostStandIn()
    project_id, _ = await app_project(server, client, server.services.dev_user_id)
    url = f"/api/projects/{project_id}/hosting"
    async with host_online(server.url, await host_token(server, host), host):
        deploy_id = await deployed(client, project_id, "staging")
        await until(lambda: settled(client, project_id, deploy_id))
        host.holds["delete"] = held = asyncio.Event()
        assert (await client.delete(url)).status_code == 200
        overview = (await client.get(url)).json()
        assert overview["deleting"] and overview["app"]
        again = await client.post(f"/api/projects/{project_id}/deploys",
                                  json={"environment": "staging", "commit": ""})  # fmt: skip
        assert again.status_code == 409 and "deleted" in again.text
        held.set()
        await until(lambda: gone(client, project_id))
