"""App Store Connect (W22a): a user's team key, kept encrypted on the server, and the client that
signs its requests with it; all against a stand-in for Apple's API."""

from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from sqlalchemy import select

from asc_standin import ISSUER_ID, KEY_ID, TEAM_ID, AscStandIn, new_key
from forge_web.apple.asc_client import AscClient, AscError, AscKey
from forge_web.db.models import AppStoreKey, AuditEntry
from support import LiveServer, dev_settings


@pytest.fixture
def asc() -> Iterator[AscStandIn]:
    with AscStandIn() as standin:
        yield standin


@pytest.fixture
def server(tmp_path: Path, asc: AscStandIn) -> Iterator[LiveServer]:
    settings = dev_settings(tmp_path / "data")
    settings.apple.asc_api_url = asc.url
    with LiveServer(settings) as live:
        yield live


@pytest.fixture
async def client(server: LiveServer) -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(base_url=server.url, headers=server.headers(), timeout=60) as c:
        yield c


def key_body(pem: str, **changes: Any) -> dict[str, Any]:
    return {"key_id": KEY_ID, "issuer_id": ISSUER_ID, "team_id": TEAM_ID, "private_key": pem,
            **changes}  # fmt: skip


async def test_the_client_signs_what_apple_checks(asc: AscStandIn) -> None:
    asc.add_app("Tally", "com.example.tally")
    good = AscClient(asc.url, AscKey(KEY_ID, ISSUER_ID, TEAM_ID, asc.pem))
    assert await good.check() == "The key works: 1 app in App Store Connect."
    asc.state.refuse_identifiers = True
    with pytest.raises(AscError, match="Provisioning"):  # Apple's own words come through
        await good.check()
    other_pem, _ = new_key()
    wrong = AscClient(asc.url, AscKey(KEY_ID, ISSUER_ID, TEAM_ID, other_pem))
    with pytest.raises(AscError) as refused:
        await wrong.get("/v1/apps")
    assert refused.value.status == 401
    await good.close()
    await wrong.close()


async def test_a_busy_apple_is_asked_again() -> None:
    answers = iter([httpx.Response(429, headers={"Retry-After": "0"}),
                    httpx.Response(200, json={"data": [], "links": {}})])  # fmt: skip
    pem, _ = new_key()
    http = httpx.AsyncClient(transport=httpx.MockTransport(lambda _: next(answers)))
    client = AscClient("https://asc.test", AscKey(KEY_ID, ISSUER_ID, TEAM_ID, pem), http)
    assert await client.get("/v1/apps") == {"data": [], "links": {}}


async def test_a_users_key_is_stored_encrypted_and_never_shown(
    server: LiveServer, client: httpx.AsyncClient, asc: AscStandIn
) -> None:
    assert (await client.get("/api/me/appstore-key")).json() is None
    saved = await client.put("/api/me/appstore-key", json=key_body(asc.pem))
    assert saved.status_code == 200, saved.text
    shown = saved.json()
    assert shown["key_id"] == KEY_ID and shown["team_id"] == TEAM_ID
    assert shown["check_ok"] is True and "The key works" in shown["check_message"]
    assert "PRIVATE KEY" not in saved.text
    listed = await client.get("/api/me/appstore-key")
    assert listed.json()["issuer_id"] == ISSUER_ID and "PRIVATE KEY" not in listed.text
    async with server.services.db.session() as session:
        row = (await session.scalars(select(AppStoreKey))).one()
        actions = [a.action for a in await session.scalars(select(AuditEntry))]
    assert "PRIVATE KEY" not in row.secret
    assert server.services.vault.decrypt(row.secret) == asc.pem
    assert "appstore.key_saved" in actions
    asc.state.refuse_identifiers = True
    again = (await client.post("/api/me/appstore-key/check")).json()
    assert again["check_ok"] is False and "Provisioning" in again["check_message"]
    assert (await client.delete("/api/me/appstore-key")).status_code == 204
    assert (await client.get("/api/me/appstore-key")).json() is None


async def test_what_is_not_an_apple_key_is_refused(client: httpx.AsyncClient) -> None:
    pem, _ = new_key()
    wrongs = [key_body("not a key"), key_body(pem, key_id="short"),
              key_body(pem, issuer_id="nope"), key_body(pem, team_id="lower12345")]  # fmt: skip
    for wrong in wrongs:
        assert (await client.put("/api/me/appstore-key", json=wrong)).status_code == 422, wrong
    rsa = (
        "-----BEGIN PRIVATE KEY-----\nMIIBVQIBADANBgkqhkiG9w0BAQEFAASCAT8wggE7AgEAAkEAq\n"
        "-----END PRIVATE KEY-----\n"
    )
    assert (await client.put("/api/me/appstore-key", json=key_body(rsa))).status_code == 422
