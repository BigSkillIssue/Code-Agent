"""Two-factor sign-in: RFC 6238 codes, setting it up, signing in with it, admins who must."""

import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select

from forge_web.auth.totp import (
    code_hash,
    new_recovery_codes,
    new_secret,
    otpauth_uri,
    secret_bytes,
    totp,
    verify,
)
from forge_web.db.models import AuditEntry, User
from forge_web.settings import load_settings
from support import LiveServer, WebClient, person

PASSWORD = "correct horse battery"
RFC_SECRETS = {
    "sha1": b"12345678901234567890",
    "sha256": b"12345678901234567890123456789012",
    "sha512": b"1234567890123456789012345678901234567890123456789012345678901234",
}
RFC_VECTORS = [  # RFC 6238 appendix B: time, then the SHA-1, SHA-256 and SHA-512 codes
    (59, "94287082", "46119246", "90693936"),
    (1111111109, "07081804", "68084774", "25091201"),
    (1111111111, "14050471", "67062674", "99943326"),
    (1234567890, "89005924", "91819424", "93441116"),
    (2000000000, "69279037", "90698825", "38618901"),
    (20000000000, "65353130", "77737706", "47863826"),
]


def test_rfc_6238_vectors() -> None:
    for at, *codes in RFC_VECTORS:
        for algorithm, code in zip(("sha1", "sha256", "sha512"), codes, strict=True):
            assert totp(RFC_SECRETS[algorithm], at, digits=8, algorithm=algorithm) == code


def test_codes_are_accepted_once_and_near_now() -> None:
    secret = new_secret()
    assert len(secret) == 32 and secret_bytes(secret) and secret == secret.upper()
    now = 1_700_000_000.0
    step = int(now // 30)
    raw = secret_bytes(secret)
    assert verify(secret, totp(raw, now), now, last_step=0) == step
    assert verify(secret, totp(raw, now - 30), now, last_step=0) == step - 1  # a slow typist
    assert verify(secret, totp(raw, now + 30), now, last_step=0) == step + 1  # a fast clock
    assert verify(secret, totp(raw, now - 90), now, last_step=0) is None
    assert verify(secret, totp(raw, now), now, last_step=step) is None  # used already
    assert verify(secret, " " + totp(raw, now)[:3] + " " + totp(raw, now)[3:], now, 0) == step
    for bad in ("", "12345", "abcdef", "1234567"):
        assert verify(secret, bad, now, last_step=0) is None
    uri = otpauth_uri(secret, "ada@example.com", "Forge")
    assert uri.startswith("otpauth://totp/Forge:ada%40example.com?")
    assert f"secret={secret}" in uri and "issuer=Forge" in uri


def test_recovery_codes() -> None:
    codes = new_recovery_codes()
    assert len(codes) == len(set(codes)) == 10
    assert all(len(c) == 9 and c[4] == "-" for c in codes)
    assert code_hash(codes[0].upper().replace("-", " ")) == code_hash(codes[0])


# With a server -----------------------------------------------------------------------------


def server_settings(data_dir: Path, **overrides: Any) -> Any:
    values = {"sandbox.isolation": "docker", **overrides}
    return load_settings(
        data_dir / "forge-web.toml", environ={"FORGE_WEB_DATA_DIR": str(data_dir)}, overrides=values
    )


@pytest.fixture
def server(tmp_path: Path) -> Iterator[LiveServer]:
    with LiveServer(server_settings(tmp_path / "data")) as live:
        yield live


async def admin_with_password(server: LiveServer, browser: WebClient) -> dict[str, Any]:
    body = {"token": server.services.setup_token, "email": "ada@example.com", "name": "Ada",
            "password": PASSWORD}  # fmt: skip
    response = await browser.post("/api/auth/setup", body)
    assert response.status_code == 200, response.text
    return dict(response.json())


def code_for(secret: str, offset: float = 0) -> str:
    return totp(secret_bytes(secret), time.time() + offset)


async def test_setting_up_and_signing_in_with_two_factor(server: LiveServer) -> None:
    async with WebClient(server) as browser:
        await admin_with_password(server, browser)
        setup = (await browser.post("/api/me/totp/setup")).json()
        secret = setup["secret"]
        assert "ada%40example.com" in setup["uri"]
        assert (await browser.post("/api/me/totp/enable", {"code": "000000"})).status_code == 400
        first_code = code_for(secret)
        enabled = await browser.post("/api/me/totp/enable", {"code": first_code})
        assert enabled.status_code == 200, enabled.text
        recovery = enabled.json()["recovery_codes"]
        assert len(recovery) == 10 and (await browser.get("/api/me")).json()["totp_enabled"]
        async with server.services.db.session() as session:
            stored = await session.scalar(select(User.totp_secret))
        assert stored and secret not in stored  # encrypted at rest
        await browser.post("/api/auth/logout")

        login = {"email": "ada@example.com", "password": PASSWORD}
        first = await browser.post("/api/auth/login", login)
        assert first.status_code == 200 and first.json() == {"totp_required": True}
        assert (await browser.get("/api/me")).status_code == 401  # no session yet
        assert (await browser.post("/api/auth/totp/verify", {"code": "123456"})).status_code == 401
        replayed = await browser.post("/api/auth/totp/verify", {"code": first_code})
        assert replayed.status_code == 401  # that code was used to turn it on
        signed_in = await browser.post("/api/auth/totp/verify", {"code": code_for(secret, 30)})
        assert signed_in.status_code == 200, signed_in.text
        assert (await browser.get("/api/me")).json()["email"] == "ada@example.com"
        await browser.post("/api/auth/logout")

        await browser.post("/api/auth/login", login)
        with_recovery = await browser.post("/api/auth/totp/verify", {"code": recovery[0]})
        assert with_recovery.status_code == 200
        await browser.post("/api/auth/logout")
        await browser.post("/api/auth/login", login)
        again = await browser.post("/api/auth/totp/verify", {"code": recovery[0]})
        assert again.status_code == 401  # each recovery code works once


async def test_turning_it_on_signs_out_other_browsers(server: LiveServer) -> None:
    async with WebClient(server) as browser, WebClient(server) as other:
        await admin_with_password(server, browser)
        login = {"email": "ada@example.com", "password": PASSWORD}
        assert (await other.post("/api/auth/login", login)).status_code == 200
        secret = (await browser.post("/api/me/totp/setup")).json()["secret"]
        assert (
            await browser.post("/api/me/totp/enable", {"code": code_for(secret)})
        ).status_code == 200
        assert (await other.get("/api/me")).status_code == 401  # maybe a stolen password
        assert (await browser.get("/api/me")).status_code == 200


async def test_a_second_factor_cannot_be_skipped(server: LiveServer) -> None:
    async with WebClient(server) as browser, WebClient(server) as other:
        await admin_with_password(server, browser)
        secret = (await browser.post("/api/me/totp/setup")).json()["secret"]
        await browser.post("/api/me/totp/enable", {"code": code_for(secret)})
        # No pending sign-in: a code alone signs nobody in.
        assert (
            await other.post("/api/auth/totp/verify", {"code": code_for(secret, 30)})
        ).status_code == 400
        await other.post("/api/auth/login", {"email": "ada@example.com", "password": "wrong one!"})
        assert (
            await other.post("/api/auth/totp/verify", {"code": code_for(secret, 30)})
        ).status_code == 400
        assert (await browser.post("/api/me/totp/disable", {"code": "000000"})).status_code == 400
        off = await browser.post("/api/me/totp/disable", {"code": code_for(secret, 30)})
        assert off.status_code == 200 and not (await browser.get("/api/me")).json()["totp_enabled"]


async def test_admins_must_use_two_factor_when_the_server_says_so(tmp_path: Path) -> None:
    settings = server_settings(tmp_path / "data", **{"auth.admin_two_factor": True})
    with LiveServer(settings) as server:
        admin = await person(server, "boss", role="admin")
        member = await person(server, "worker")
        refused = await admin.web.get("/api/admin/users")
        assert refused.status_code == 403 and "two-factor" in refused.json()["detail"]
        me = (await admin.web.get("/api/me")).json()
        assert me["two_factor_required"] and not me["totp_enabled"]
        secret = (await admin.web.post("/api/me/totp/setup")).json()["secret"]
        assert (
            await admin.web.post("/api/me/totp/enable", {"code": code_for(secret)})
        ).status_code == 200
        assert (await admin.web.get("/api/admin/users")).status_code == 200
        keep = await admin.web.post("/api/me/totp/disable", {"code": code_for(secret, 30)})
        assert keep.status_code == 409  # admins keep it while the server requires it
        # A member who lost their phone: an admin turns it off for them.
        member_secret = (await member.web.post("/api/me/totp/setup")).json()["secret"]
        await member.web.post("/api/me/totp/enable", {"code": code_for(member_secret)})
        listed = {u["id"]: u for u in (await admin.web.get("/api/admin/users")).json()}
        assert listed[member.id]["totp_enabled"] and listed[admin.id]["totp_enabled"]
        reset = await admin.web.post(f"/api/admin/users/{member.id}/totp/reset")
        assert reset.status_code == 200
        assert (await member.web.get("/api/me")).status_code == 401  # signed out everywhere
        async with server.services.db.session() as session:
            row = await session.get(User, member.id)
        assert row is not None and not row.totp_enabled and row.totp_secret is None
        async with server.services.db.session() as session:
            actions = list(await session.scalars(select(AuditEntry.action)))
        assert {"totp_enabled", "totp_reset"} <= set(actions)
        for who in (admin, member):
            await who.web.client.aclose()
