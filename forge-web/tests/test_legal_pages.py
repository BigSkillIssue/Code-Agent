"""The legal pages of hosted apps (W28): going live needs the operator's details, and the edge's
Impressum and privacy page show exactly them, escaped, for the app the request names."""

from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import httpx
import pytest

from host_standin import HostStandIn, host_online
from support import LiveServer
from test_hosting_deploy import (
    OPERATOR,
    app_project,
    deployed,
    host_token,
    settings_for,
    settled,
    short_polls,
    until,
)

__all__ = ["short_polls"]


@pytest.fixture
def server(tmp_path: Path) -> Iterator[LiveServer]:
    settings = settings_for(tmp_path / "data")
    settings.hosting.provider_name = "Beispiel Hosting GmbH"
    with LiveServer(settings) as live:
        yield live


@pytest.fixture
async def client(server: LiveServer) -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(base_url=server.url, headers=server.headers(), timeout=60) as c:
        yield c


async def staged(server: LiveServer, client: httpx.AsyncClient, operator: bool) -> str:
    """An app project live in staging; its id."""
    host = HostStandIn()
    project_id, _ = await app_project(server, client, server.services.dev_user_id, operator)
    async with host_online(server.url, await host_token(server, host), host):
        deploy_id = await deployed(client, project_id, "staging")
        assert (await until(lambda: settled(client, project_id, deploy_id)))["status"] == "live"
    return project_id


async def test_going_live_needs_the_operators_details(
    server: LiveServer, client: httpx.AsyncClient
) -> None:
    project_id = await staged(server, client, operator=False)
    url = f"/api/projects/{project_id}/deploys"
    refused = await client.post(url, json={"environment": "production"})
    assert refused.status_code == 409 and "Impressum" in refused.text
    test_page = await client.get("/legal/shop/impressum")
    assert test_page.status_code == 200 and "Testversion" in test_page.text  # staging only
    bad = await client.put(f"/api/projects/{project_id}/hosting/operator",
                           json={**OPERATOR, "email": "not an address"})  # fmt: skip
    assert bad.status_code == 422
    saved = await client.put(f"/api/projects/{project_id}/hosting/operator", json=OPERATOR)
    assert saved.status_code == 200 and saved.json()["name"] == "Ada Lovelace"
    shown = (await client.get(f"/api/projects/{project_id}/hosting")).json()
    assert shown["operator"]["email"] == "ada@example.com"


async def test_the_legal_pages_show_the_creators_details(
    server: LiveServer, client: httpx.AsyncClient
) -> None:
    project_id = await staged(server, client, operator=True)
    details = {**OPERATOR, "name": "<script>alert(1)</script> Shop GmbH",
               "representative": "vertreten durch Ada Lovelace", "phone": "+49 30 123456",
               "trade_register": "Amtsgericht Berlin HRB 12345", "vat_id": "DE123456789",
               "privacy_email": "datenschutz@example.com",
               "privacy_extra": "Wir versenden einen Newsletter nur mit Einwilligung."}  # fmt: skip
    url = f"/api/projects/{project_id}/hosting/operator"
    assert (await client.put(url, json=details)).status_code == 200
    async with httpx.AsyncClient(base_url=server.url, timeout=30) as anyone:  # no sign-in
        imprint = await anyone.get("/legal/shop/impressum")
        privacy = await anyone.get("/legal/shop/datenschutz")
        unknown = await anyone.get("/legal/nobody/impressum")
    assert imprint.status_code == 200 and "text/html" in imprint.headers["content-type"]
    page = imprint.text
    assert "&lt;script&gt;alert(1)&lt;/script&gt; Shop GmbH" in page and "<script>" not in page
    for shown in ("Hauptstraße 1<br>10115 Berlin", "mailto:ada@example.com", "+49 30 123456",
                  "Amtsgericht Berlin HRB 12345", "DE123456789", "vertreten durch Ada Lovelace",
                  "§ 5 DDG", 'href="/melden"', "Beispiel Hosting GmbH"):  # fmt: skip
        assert shown in page, shown
    assert "default-src 'none'" in imprint.headers["content-security-policy"]
    assert 'name="robots" content="noindex"' in page
    text = privacy.text
    for shown in ("datenschutz@example.com", "Beispiel Hosting GmbH", "Art. 28 DSGVO",
                  "Art. 15–21 DSGVO", "Newsletter nur mit Einwilligung"):  # fmt: skip
        assert shown in text, shown
    assert unknown.status_code == 404
