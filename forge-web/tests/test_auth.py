"""Accounts: first admin, sign-in, CSRF, origins, sign-up modes, invites, resets, audit."""

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select

from forge_web.auth import onetime
from forge_web.auth.passwords import password_problem
from forge_web.db.models import AuditEntry, User
from forge_web.settings import load_settings
from support import LiveServer, WebClient

PASSWORD = "correct horse battery"


def server_settings(data_dir: Path, **overrides: Any) -> Any:
    values = {"sandbox.isolation": "docker", **overrides}
    return load_settings(
        data_dir / "forge-web.toml", environ={"FORGE_WEB_DATA_DIR": str(data_dir)}, overrides=values
    )


@pytest.fixture
def server(tmp_path: Path) -> Iterator[LiveServer]:
    with LiveServer(server_settings(tmp_path / "data")) as live:
        yield live


async def make_admin(server: LiveServer, browser: WebClient) -> dict[str, Any]:
    token = server.services.setup_token
    body = {"token": token, "email": "Admin@Example.com", "name": "Ada", "password": PASSWORD}
    response = await browser.post("/api/auth/setup", body)
    assert response.status_code == 200, response.text
    return dict(response.json())


def test_password_rules() -> None:
    assert password_problem("short") is not None
    assert password_problem("aaaaaaaaaaaa") is not None
    assert password_problem(PASSWORD) is None


async def test_first_admin_with_the_setup_token(server: LiveServer) -> None:
    async with WebClient(server) as browser:
        assert (await browser.get("/api/auth/config")).json()["setup_needed"]
        wrong = {"token": "nope", "email": "a@example.com", "password": PASSWORD}
        assert (await browser.post("/api/auth/setup", wrong)).status_code == 403
        admin = await make_admin(server, browser)
        assert admin["role"] == "admin" and admin["email"] == "admin@example.com"
        assert (await browser.get("/api/me")).json()["id"] == admin["id"]
        again = {"token": "x", "email": "b@example.com", "password": PASSWORD}
        assert (await browser.post("/api/auth/setup", again)).status_code == 409
        assert not (await browser.get("/api/auth/config")).json()["setup_needed"]


async def test_sign_in_sign_out_and_rate_limit(server: LiveServer) -> None:
    async with WebClient(server) as setup:
        await make_admin(server, setup)
    async with WebClient(server) as browser:
        bad = {"email": "admin@example.com", "password": "wrong password!"}
        assert (await browser.post("/api/auth/login", bad)).status_code == 401
        good = {"email": "ADMIN@example.com", "password": PASSWORD}
        assert (await browser.post("/api/auth/login", good)).status_code == 200
        assert (await browser.get("/api/me")).status_code == 200
        assert (await browser.post("/api/auth/logout")).status_code == 204
        assert (await browser.get("/api/me")).status_code == 401
        codes = [(await browser.post("/api/auth/login", bad)).status_code for _ in range(12)]
        assert codes[-1] == 429
    async with server.services.db.session() as session:
        actions = list(await session.scalars(select(AuditEntry.action)))
    assert {"setup", "login", "login_failed", "logout"} <= set(actions)


async def test_changing_requests_need_the_csrf_value(server: LiveServer) -> None:
    async with WebClient(server) as browser:
        await make_admin(server, browser)
        without = await browser.client.post("/api/projects", json={"name": "x"})
        assert without.status_code == 403 and "CSRF" in without.json()["detail"]
        wrong = await browser.client.post(
            "/api/projects", json={"name": "x"}, headers={"X-CSRF-Token": "forged"}
        )
        assert wrong.status_code == 403
        assert (await browser.get("/api/projects")).status_code == 200  # reading needs none


async def test_cross_site_requests_are_refused(server: LiveServer) -> None:
    async with WebClient(server, origin="https://evil.example") as evil:
        login = await evil.post("/api/auth/login", {"email": "a@b.cd", "password": PASSWORD})
        assert login.status_code == 403 and "cross-site" in login.json()["detail"]
    async with WebClient(server, origin=server.url) as same_site:
        await make_admin(server, same_site)
        assert (await same_site.post("/api/projects", {"name": "ok"})).status_code != 403
        cookie = "; ".join(f"{k}={v}" for k, v in same_site.client.cookies.items())
    from websockets.asyncio.client import connect
    from websockets.exceptions import InvalidStatus

    headers = {"Cookie": cookie, "Origin": "https://evil.example"}
    with pytest.raises(InvalidStatus):
        await connect(server.ws_url, additional_headers=headers)
    same = {"Cookie": cookie, "Origin": server.url}
    async with connect(server.ws_url, additional_headers=same) as ws:
        assert '"hello"' in str(await ws.recv())


async def test_invite_only_sign_up(server: LiveServer) -> None:
    async with WebClient(server) as admin:
        await make_admin(server, admin)
    db = server.services.db
    async with WebClient(server) as stranger:
        body = {"email": "bob@example.com", "password": PASSWORD}
        assert (await stranger.post("/api/auth/signup", body)).status_code == 403
        token = await onetime.issue(db, "invite", email="bob@example.com")
        info = (await stranger.get(f"/api/auth/invite/{token}")).json()
        assert info["email"] == "bob@example.com"
        eve = {**body, "email": "eve@example.com", "invite": token}
        other = await stranger.post("/api/auth/signup", eve)
        assert other.status_code == 403  # the invite names someone else
        joined = await stranger.post("/api/auth/signup", {**body, "invite": token})
        assert joined.status_code == 201 and joined.json()["status"] == "active"
        assert (await stranger.get("/api/me")).json()["email"] == "bob@example.com"
        reuse = {**body, "email": "x@example.com", "invite": token}
        again = await stranger.post("/api/auth/signup", reuse)
        assert again.status_code == 403  # used up


async def test_approval_mode_waits_for_an_admin(tmp_path: Path) -> None:
    with LiveServer(server_settings(tmp_path / "data", **{"auth.signup": "approval"})) as server:
        async with WebClient(server) as admin:
            await make_admin(server, admin)
        async with WebClient(server) as newcomer:
            body = {"email": "carl@example.com", "password": PASSWORD}
            signed = await newcomer.post("/api/auth/signup", body)
            assert signed.status_code == 201 and signed.json()["status"] == "pending"
            login = await newcomer.post("/api/auth/login", body)
            assert login.status_code == 403 and "approve" in login.json()["detail"]


async def test_open_mode_with_allowed_domains(tmp_path: Path) -> None:
    overrides = {"auth.signup": "open", "auth.allowed_domains": ["example.com"]}
    with LiveServer(server_settings(tmp_path / "data", **overrides)) as server:
        async with WebClient(server) as admin:
            await make_admin(server, admin)
        async with WebClient(server) as visitor:
            outside = {"email": "dan@other.org", "password": PASSWORD}
            assert (await visitor.post("/api/auth/signup", outside)).status_code == 403
            inside = {"email": "dan@example.com", "password": PASSWORD}
            assert (await visitor.post("/api/auth/signup", inside)).json()["status"] == "active"
            assert (await visitor.post("/api/auth/signup", inside)).status_code == 409


async def test_password_reset_link_signs_out_everywhere(server: LiveServer) -> None:
    async with WebClient(server) as first:
        admin = await make_admin(server, first)
        token = await onetime.issue(server.services.db, "reset", user_id=admin["id"])
        async with WebClient(server) as second:
            new = {"token": token, "password": "a brand new passphrase"}
            assert (await second.post("/api/auth/reset", new)).status_code == 200
            assert (await second.get("/api/me")).status_code == 200
            assert (await second.post("/api/auth/reset", new)).status_code == 400  # once only
        assert (await first.get("/api/me")).status_code == 401  # the old session ended
        login = {"email": "admin@example.com", "password": "a brand new passphrase"}
        assert (await first.post("/api/auth/login", login)).status_code == 200


async def test_weak_passwords_and_bad_emails_are_refused(server: LiveServer) -> None:
    async with WebClient(server) as browser:
        token = server.services.setup_token
        weak = {"token": token, "email": "a@example.com", "password": "short"}
        assert (await browser.post("/api/auth/setup", weak)).status_code == 422
        bad = {"token": token, "email": "not-an-email", "password": PASSWORD}
        assert (await browser.post("/api/auth/setup", bad)).status_code == 422
    async with server.services.db.session() as session:
        assert list(await session.scalars(select(User))) == []
