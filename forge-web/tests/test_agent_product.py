"""Forge's agent on Forge's servers (W54): it makes a product, deploys it, reads its logs and its
database and fetches its pages through the gateway, for its own project only; production only
where the admin allows it."""

from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from sqlalchemy import select

from forge_sandbox.product_mcp import Tools
from forge_sandbox.products.place import create
from forge_web.db.hosting_models import Deploy
from forge_web.db.models import AuditEntry, Chat, Project
from forge_web.hosting import agent_api
from host_standin import HostStandIn, host_online
from support import LiveServer, person
from test_hosting_deploy import (  # the same stand-in host and helpers as the people's deploys
    DOMAIN,
    OPERATOR,
    git,
    host_token,
    settings_for,
    short_polls,  # noqa: F401 (an autouse fixture)
    until,
)


@pytest.fixture
def server(tmp_path: Path) -> Iterator[LiveServer]:
    with LiveServer(settings_for(tmp_path / "data")) as live:
        live.services.gateway.chat_is_working = lambda _chat_id: True  # the chats are working
        yield live


@pytest.fixture
async def gateway(server: LiveServer) -> AsyncIterator[httpx.AsyncClient]:
    # The gateway's own socket: its routes run in the server's loop, as in the sandbox.
    transport = httpx.AsyncHTTPTransport(uds=str(server.services.gateway_server.path))
    async with httpx.AsyncClient(transport=transport, base_url="http://gw", timeout=60) as client:
        yield client


async def project_with_product(server: LiveServer, web: Any, name: str) -> tuple[str, str, str]:
    """An empty project into which the agent put a website and committed it: id, chat, HEAD."""
    made = await web.request("POST", "/api/projects", json={"name": name})
    assert made.status_code == 201, made.text
    project_id = made.json()["id"]
    workspace = server.services.driver.workspace(project_id)
    create(workspace, "site", name.lower(), None, (), DOMAIN)
    git(workspace, "add", "-A")
    git(workspace, "commit", "-q", "-m", "the agent made a website")
    async with server.services.db.session() as session:
        chat = await session.scalar(select(Chat.id).where(Chat.project_id == project_id))
    return project_id, str(chat), git(workspace, "rev-parse", "HEAD")


def run_header(server: LiveServer, chat_id: str) -> dict[str, str]:
    chat = Chat(id=chat_id, token_generation=1)
    return {"Authorization": f"Bearer {server.services.gateway.token_for(chat)}"}


async def status(gateway: httpx.AsyncClient, headers: dict[str, str]) -> dict[str, Any]:
    reply = await gateway.get("/product/status", headers=headers)
    assert reply.status_code == 200, reply.text
    found: dict[str, Any] = reply.json()
    return found


async def test_the_agent_deploys_its_product_and_works_on_it(
    server: LiveServer, gateway: httpx.AsyncClient, client: Any = None
) -> None:
    host = HostStandIn()
    admin = server.services.dev_user_id
    async with httpx.AsyncClient(base_url=server.url, headers=server.headers(), timeout=60) as web:
        project_id, chat, head = await project_with_product(server, web, "Blumen")
    headers = run_header(server, chat)
    async with host_online(server.url, await host_token(server, host), host):
        started = await gateway.post("/product/deploy", headers=headers,
                                     json={"environment": "staging", "commit": head})  # fmt: skip
        assert started.status_code == 200, started.text
        async with server.services.db.session() as session:
            assert (await session.get(Project, project_id)).kind == "app"  # a product now

        async def live() -> Any:
            deploys = (await status(gateway, headers)).get("deploys") or []
            return next((d for d in deploys if d["status"] == "live"), None)

        assert (await until(live))["commit"] == head
        logs = await gateway.post("/product/logs", headers=headers, json={})
        assert logs.status_code == 200 and logs.json()["ok"] is True
        query = {"query": "select count(*) from accounts", "environment": "staging"}
        sql = await gateway.post("/product/sql", headers=headers, json=query)
        assert sql.status_code == 200 and host.last("sql").sql.query == query["query"]
        assert host.last("sql").sql.write is False and host.last("sql").app == "blumen"
        production = {"environment": "production", "commit": head}
        refused = await gateway.post("/product/deploy", headers=headers, json=production)
        assert refused.status_code == 403  # the admin did not allow production
        assert (
            await gateway.post("/product/logs", headers=headers, json={"environment": "production"})
        ).status_code == 403
    async with server.services.db.session() as session:
        actions = set(await session.scalars(select(AuditEntry.action)))
    assert {"agent.deploy", "agent.logs", "agent.sql"} <= actions
    assert admin


async def test_an_agent_reaches_only_its_own_product(
    server: LiveServer, gateway: httpx.AsyncClient
) -> None:
    host = HostStandIn()
    async with httpx.AsyncClient(base_url=server.url, headers=server.headers(), timeout=60) as web:
        _, chat_a, head_a = await project_with_product(server, web, "Laden")
    maria = await person(server, "maria")
    project_b, chat_b, _ = await project_with_product(server, maria.web, "Garten")
    a, b = run_header(server, chat_a), run_header(server, chat_b)
    async with host_online(server.url, await host_token(server, host), host):
        deploy = {"environment": "staging", "commit": head_a}
        assert (await gateway.post("/product/deploy", headers=a, json=deploy)).status_code == 200
        for path, body in (("/product/logs", {}), ("/product/sql", {"query": "select 1"}),
                           ("/product/fetch", {"path": "/"})):  # fmt: skip
            reply = await gateway.post(path, headers=b, json=body)
            assert reply.status_code == 409 and "not hosted yet" in reply.text, path
        # A's commit named by B's agent deploys B's own project (where packing it will fail).
        foreign = await gateway.post("/product/deploy", headers=b, json=deploy)
        async with server.services.db.session() as session:
            row = await session.get(Deploy, foreign.json()["deploy"])
            assert row is not None and row.project_id == project_b
    no_token = await gateway.get("/product/status")
    assert no_token.status_code == 401


async def test_production_where_the_admin_allows_it_and_pages_of_the_product(
    server: LiveServer, gateway: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    host = HostStandIn()
    async with httpx.AsyncClient(base_url=server.url, headers=server.headers(), timeout=60) as web:
        project_id, chat, head = await project_with_product(server, web, "Atelier")
        operator = f"/api/projects/{project_id}/hosting/operator"
        await web.request("POST", "/api/projects", json={"name": "x"})
    headers = run_header(server, chat)
    server.services.settings.hosting.agent_production = True
    asked: list[str] = []

    async def fetched(url: str) -> dict[str, Any]:
        asked.append(url)
        return {"ok": True, "status": 200, "text": "<h1>Atelier</h1>"}

    monkeypatch.setattr(agent_api, "fetched", fetched)
    async with host_online(server.url, await host_token(server, host), host):
        staging = {"environment": "staging", "commit": head}
        assert (
            await gateway.post("/product/deploy", headers=headers, json=staging)
        ).status_code == 200

        async def live(environment: str) -> Any:
            deploys = (await status(gateway, headers)).get("deploys") or []
            return next((d for d in deploys if d["status"] == "live"
                         and d["environment"] == environment), None)  # fmt: skip

        await until(lambda: live("staging"))
        production = {"environment": "production", "commit": head}
        missing = await gateway.post("/product/deploy", headers=headers, json=production)
        assert missing.status_code == 409 and "Impressum" in missing.text  # still the user's part
        async with httpx.AsyncClient(base_url=server.url, headers=server.headers()) as web:
            assert (await web.request("PUT", operator, json=OPERATOR)).status_code == 200
        started = await gateway.post("/product/deploy", headers=headers, json=production)
        assert started.status_code == 200, started.text
        await until(lambda: live("production"))
        page = await gateway.post(
            "/product/fetch",
            headers=headers,
            json={"environment": "production", "path": "/about?x=1"},
        )
        assert page.json()["text"] == "<h1>Atelier</h1>"
        assert asked == ["https://atelier.apps.example/about?x=1"]


def test_the_tools_refuse_uncommitted_work_and_foreign_pages(tmp_path: Path) -> None:
    create(tmp_path, "site", "atelier", None, (), DOMAIN)
    git(tmp_path, "init", "-q")
    tools = Tools(tmp_path, "", "FW_GATEWAY_TOKEN", DOMAIN)
    with pytest.raises(Exception, match="commit your changes"):
        tools.head()
    git(tmp_path, "add", "-A")
    git(tmp_path, "commit", "-q", "-m", "site")
    assert len(tools.head()) == 40
    assert tools.own_hosts() == {"atelier.apps.example", "atelier.staging.apps.example"}


async def test_forge_gets_every_product_tool_and_places_a_product(tmp_path: Path) -> None:
    from forge_sandbox.methods import ChatOptions
    from forge_sandbox.product_mcp import server as product_server
    from forge_sandbox.worker import PRODUCT_TOOLS, config_overrides

    mcp = product_server(Tools(tmp_path, "", "FW_GATEWAY_TOKEN", DOMAIN))
    names = {tool.name for tool in await mcp.list_tools()}
    assert names == set(PRODUCT_TOOLS)
    result = await mcp.call_tool("product_create", {"kind": "site", "name": "atelier"})
    assert "written:" in str(result) and (tmp_path / "forge.app.toml").is_file()
    overrides = config_overrides(ChatOptions(product_url="http://127.0.0.1:47101"), tmp_path)
    command = overrides["mcp_servers.forge_web"]["command"]
    assert command[1:3] == ["-m", "forge_sandbox.product_mcp"]
    assert command[-2:] == ["--gateway", "http://127.0.0.1:47101"]
    assert "mcp__forge_web__deploy" in overrides["permissions.allow"]
