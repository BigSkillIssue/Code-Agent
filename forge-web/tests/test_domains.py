"""Customers' own domains (W30): without the TXT record no certificate is asked for; a verified
domain serves the app, with the edge's legal pages before its own routes; a domain belongs to
one app only, and the server's own domains are no customer's; a domain given up loses its
certificate. DNS is a DNS-over-HTTPS stand-in on localhost, the host the stand-in host."""

from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest

from dns_standin import DohStandIn
from docker_standin import JOB_ID, FakeDocker, release_job, runner_for, tarball
from forge_hostworker.edge import LEGAL_PATHS, Edge
from forge_hostworker.wire import HostJob
from forge_web.hosting.domains import normal_domain, txt_records
from host_standin import HostStandIn, host_online
from support import LiveServer
from test_host_edge import DOMAIN, LEGAL, Caddy, ask, hosts_in
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
OWN = "shop.example.org"


# On the host --------------------------------------------------------------------------------


def domains_job(*domains: str) -> HostJob:
    return HostJob(id=JOB_ID, kind="domains", app="shop", environment="staging",
                   domains=list(domains))  # fmt: skip


def certificate(storage: Path, host: str) -> Path:
    folder = storage / "certificates" / "acme-v02.api.letsencrypt.org-directory" / host
    folder.mkdir(parents=True)
    (folder / f"{host}.key").write_text("-----BEGIN EC PRIVATE KEY-----")
    return folder


async def test_only_a_verified_domain_gets_a_certificate_and_the_legal_pages(
    tmp_path: Path,
) -> None:
    docker, caddy, storage = FakeDocker(), Caddy(), tmp_path / "caddy"
    runner = runner_for(tmp_path, docker)
    edge = Edge(runner, docker, DOMAIN, LEGAL, certificates=storage, transport=caddy.transport())
    runner.on_change = edge.changed
    assert (await runner.run(release_job(), tarball(tmp_path / "s.tgz"), {})).ok
    server = await edge.serve_ask(port=0)
    port = server.sockets[0].getsockname()[1]
    try:
        assert await ask(port, f"/ask?domain={OWN}") == 404  # not verified: no certificate
        assert (await runner.run(domains_job(OWN), None, {})).ok
        assert await ask(port, f"/ask?domain={OWN}") == 200
        assert sorted(hosts_in(caddy.loaded[-1])) == [OWN, "shop.staging.apps.example"]
        [route] = caddy.loaded[-1]["apps"]["http"]["servers"]["apps"]["routes"]
        legal = route["handle"][1]["routes"][0]
        assert legal["match"] == [{"path": list(LEGAL_PATHS)}]  # first, for the own domain too
        kept = certificate(storage, OWN)
        await edge.refresh()
        assert kept.is_dir()
        assert (await runner.run(domains_job(), None, {})).ok  # given up
        assert await ask(port, f"/ask?domain={OWN}") == 404
        assert not kept.exists()
    finally:
        server.close()
        await edge.close()


def test_only_plain_domain_names_reach_the_host() -> None:
    for bad in ("shop.example.org/evil", "*.example.org", "localhost", "shop..example.org"):
        with pytest.raises(ValueError):
            domains_job(bad)


# On the server ------------------------------------------------------------------------------


def test_the_servers_own_domains_are_no_customers(tmp_path: Path) -> None:
    settings = settings_for(tmp_path / "data")
    settings.server.public_url = "https://forge.example.com"
    settings.preview.domain = "preview.example.net"
    assert normal_domain(" HTTPS://Shop.Example.org/ ", settings) == OWN
    for taken in ("x.apps.example", "apps.example", "forge.example.com",
                  "p3000-x.preview.example.net", "not a domain", "localhost"):  # fmt: skip
        with pytest.raises(ValueError):
            normal_domain(taken, settings)


async def test_txt_records_come_whole_from_dns_over_https() -> None:
    def resolver(request: httpx.Request) -> httpx.Response:
        assert request.url.params["type"] == "TXT"
        answer = [{"type": 16, "data": '"forge-verify=" "abc"'}, {"type": 5, "data": "cname."}]
        return httpx.Response(200, json={"Status": 0, "Answer": answer})

    found = await txt_records("https://dns.example/dns-query", f"_forge-verify.{OWN}",
                              transport=httpx.MockTransport(resolver))  # fmt: skip
    assert found == ["forge-verify=abc"]


@pytest.fixture
def dns() -> Iterator[DohStandIn]:
    with DohStandIn() as stand_in:
        yield stand_in


@pytest.fixture
def server(tmp_path: Path, dns: DohStandIn) -> Iterator[LiveServer]:
    settings = settings_for(tmp_path / "data")
    settings.hosting.dns_resolver = dns.url
    with LiveServer(settings) as live:
        yield live


@pytest.fixture
async def client(server: LiveServer) -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(base_url=server.url, headers=server.headers(), timeout=60) as c:
        yield c


async def domains_jobs(host: HostStandIn) -> list[list[str]]:
    return [job.domains for job in host.jobs if job.kind == "domains"]


async def test_a_domain_is_served_only_after_its_txt_record(
    server: LiveServer, client: httpx.AsyncClient, dns: DohStandIn
) -> None:
    host = HostStandIn()
    project_id, _ = await app_project(server, client, server.services.dev_user_id)
    url = f"/api/projects/{project_id}/hosting/domains"
    body = {"domain": OWN, "environment": "staging"}
    assert (await client.post(url, json=body)).status_code == 409  # no app on a host yet
    async with host_online(server.url, await host_token(server, host), host):
        deploy_id = await deployed(client, project_id, "staging")
        await until(lambda: settled(client, project_id, deploy_id))
        added = (await client.post(url, json=body)).json()
        assert added["txt_name"] == f"_forge-verify.{OWN}" and added["verified_at"] == 0
        assert added["txt_value"].startswith("forge-verify=")
        assert added["points_to"].endswith(".staging.apps.example")
        first = (await client.post(f"{url}/{OWN}/check")).json()
        assert first["verified_at"] == 0 and "no TXT record" in first["error"]
        assert dns.asked == [f"_forge-verify.{OWN}"] and await domains_jobs(host) == []
        dns.records[f"_forge-verify.{OWN}"] = ["v=spf1 -all", added["txt_value"]]
        second = (await client.post(f"{url}/{OWN}/check")).json()
        assert second["verified_at"] > 0 and second["error"] == ""
        assert await until(lambda: domains_jobs(host)) == [[OWN]]
        assert [d["domain"] for d in (await client.get(url)).json()] == [OWN]
        assert (await client.delete(f"{url}/{OWN}")).json() == {"removed": True}
        await until(lambda: has_jobs(host, 2))
    assert await domains_jobs(host) == [[OWN], []]  # given up: the host forgets it


async def has_jobs(host: HostStandIn, count: int) -> bool:
    return len(await domains_jobs(host)) >= count


async def staged(client: httpx.AsyncClient, project_id: str) -> None:
    """The project's app deployed to staging, once that deploy ended."""
    deploy_id = await deployed(client, project_id, "staging")
    await until(lambda: settled(client, project_id, deploy_id))


async def test_a_domain_belongs_to_one_app_only(
    server: LiveServer, client: httpx.AsyncClient
) -> None:
    host = HostStandIn()
    first, _ = await app_project(server, client, server.services.dev_user_id)
    second, _ = await app_project(server, client, server.services.dev_user_id)
    body: dict[str, Any] = {"domain": OWN, "environment": "staging"}
    async with host_online(server.url, await host_token(server, host), host):
        for project_id in (first, second):
            await staged(client, project_id)
        mine = f"/api/projects/{first}/hosting/domains"
        assert (await client.post(mine, json=body)).status_code == 200
        theirs = f"/api/projects/{second}/hosting/domains"
        taken = await client.post(theirs, json=body | {"domain": OWN.upper()})
        assert taken.status_code == 409 and "belongs to an app" in taken.text
        assert (await client.post(f"{theirs}/{OWN}/check")).status_code == 404
        assert (await client.delete(f"{theirs}/{OWN}")).status_code == 404
        ours = await client.post(mine, json=body | {"domain": "x.apps.example"})
        assert ours.status_code == 422
