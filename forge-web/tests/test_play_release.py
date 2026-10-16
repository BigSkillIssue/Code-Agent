"""Google Play releases (W37b): a bundle is signed with the app's upload key on this server;
every token is checked by the stand-in (tests/play_standin.py); nothing is released without
the approval of the commit and an admin's (or a trusted creator's) go; a personal account is
told about the closed test; an app's first release stays a draft; Google's refusals come back
with what to do."""

import base64
import hashlib
import io
import shutil
import subprocess
import time
import zipfile
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.serialization import pkcs7
from sqlalchemy import select, update

from forge_web.db.hosting_models import GoLiveApproval
from forge_web.db.store_models import PlayKey
from forge_web.stores.play import PlayClient, PlayError, ServiceAccount, assertion
from forge_web.stores.play_signing import fingerprint, new_upload_key, sign_bundle
from host_standin import HostStandIn, host_online
from play_standin import PlayStandIn, signer_sha256
from support import LiveServer, Person, person
from test_android_build import android_project, finished, server_settings, started
from test_hosting_deploy import host_token, short_polls, until

__all__ = ["short_polls"]
PACKAGE = "com.example.shop"


def unsigned(path: Path) -> Path:
    with zipfile.ZipFile(path, "w") as bundle:
        bundle.writestr("base/manifest/AndroidManifest.xml", "versionCode=1\n")
        bundle.writestr("base/res/" + "a-long-folder-name/" * 6 + "icon.png", b"PNG")
        bundle.writestr("META-INF/OLD.SF", b"an earlier signature")
    return path


def test_a_bundle_is_signed_with_the_apps_upload_key(tmp_path: Path) -> None:
    key, certificate = new_upload_key("shop")
    years = x509.load_pem_x509_certificate(certificate.encode())
    assert (years.not_valid_after_utc - years.not_valid_before_utc).days > 25 * 365
    sign_bundle(unsigned(tmp_path / "u.aab"), tmp_path / "s.aab", key, certificate)
    with zipfile.ZipFile(tmp_path / "s.aab") as bundle:
        names = bundle.namelist()
        assert names[:3] == ["META-INF/MANIFEST.MF", "META-INF/FORGE.SF", "META-INF/FORGE.RSA"]
        assert "META-INF/OLD.SF" not in names  # an earlier signature is replaced
        manifest = bundle.read("META-INF/MANIFEST.MF").decode().replace("\r\n ", "")
        dex = base64.b64encode(hashlib.sha256(b"PNG").digest()).decode()
        assert f"icon.png\r\nSHA-256-Digest: {dex}" in manifest
        assert all(len(line) <= 72 for line in bundle.read("META-INF/MANIFEST.MF").split(b"\r\n"))
        signature = bundle.read("META-INF/FORGE.SF")
        whole = base64.b64encode(hashlib.sha256(bundle.read("META-INF/MANIFEST.MF")).digest())
        assert b"SHA-256-Digest-Manifest: " + whole in signature
        [signer] = pkcs7.load_der_pkcs7_certificates(bundle.read("META-INF/FORGE.RSA"))
    assert signer.fingerprint(hashes.SHA256()).hex(":").upper() == fingerprint(certificate)
    assert signer_sha256((tmp_path / "s.aab").read_bytes()) == fingerprint(certificate)
    if shutil.which("jarsigner"):  # where a JDK is at hand, Java's own check agrees
        checked = subprocess.run(["jarsigner", "-verify", str(tmp_path / "s.aab")],
                                 capture_output=True, text=True, check=False)  # fmt: skip
        assert "jar verified." in checked.stdout, checked.stdout + checked.stderr


@pytest.fixture
def play() -> Iterator[PlayStandIn]:
    with PlayStandIn() as standin:
        yield standin


async def test_every_token_is_checked_by_the_stand_in(play: PlayStandIn) -> None:
    play.add_app(PACKAGE)
    account = ServiceAccount.model_validate(play.service_account(PACKAGE))
    client = PlayClient(play.url, play.token_url, account)
    assert "may publish" in await client.check(PACKAGE)
    await client.close()
    stranger = ServiceAccount.model_validate(play.service_account(PACKAGE))
    forged = stranger.model_copy(update={"private_key": account.private_key})  # not its key
    with pytest.raises(PlayError, match="Invalid JWT Signature"):
        await PlayClient(play.url, play.token_url, forged).bearer()
    elsewhere = httpx.post(play.token_url, data={  # a token meant for another audience
        "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
        "assertion": assertion(account, "https://example.com/token", time.time()),
    })  # fmt: skip
    assert elsewhere.status_code == 400 and "audience" in elsewhere.text
    no_token = httpx.post(f"{play.url}/androidpublisher/v3/applications/{PACKAGE}/edits")
    assert no_token.status_code == 401
    outsider = ServiceAccount.model_validate(play.service_account("com.other.app"))
    with pytest.raises(PlayError, match="does not have permission"):
        await PlayClient(play.url, play.token_url, outsider).check(PACKAGE)


def settings_with(data: Path, play: PlayStandIn) -> Any:
    settings = server_settings(data)
    settings.stores.play_api_url, settings.stores.play_token_url = play.url, play.token_url
    return settings


@pytest.fixture
def server(tmp_path: Path, play: PlayStandIn) -> Iterator[LiveServer]:
    with LiveServer(settings_with(tmp_path / "data", play)) as live:
        yield live


@pytest.fixture
async def client(server: LiveServer) -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(base_url=server.url, headers=server.headers(), timeout=60) as c:
        yield c


@pytest.fixture
def host() -> Iterator[HostStandIn]:
    standin = HostStandIn()
    yield standin
    shutil.rmtree(standin.data_dir, ignore_errors=True)


async def save_key(web: Any, key_file: dict[str, str], account: str = "organisation") -> Any:
    saved = await web.request("PUT", "/api/me/play-key",
                              json={"key_file": key_file, "account": account})  # fmt: skip
    assert saved.status_code == 200, saved.text
    return saved.json()


async def built_app(server: LiveServer, web: Any) -> tuple[str, str]:
    """A project live in staging with a built Android app; its id and the build's."""
    project_id = await android_project(server, web)
    build = await started(web, project_id)
    done = await until(lambda: finished(web, project_id, build["id"]))
    assert done["status"] == "built", done["error"]
    return project_id, str(build["id"])


async def release(web: Any, project_id: str, build_id: str, **body: Any) -> httpx.Response:
    return await web.request("POST", f"/api/projects/{project_id}/android/releases",
                             json={"build_id": build_id, **body})  # fmt: skip


async def settled(web: Any, project_id: str, release_id: str) -> Any:
    rows = (await web.get(f"/api/projects/{project_id}/android/releases")).json()
    row = next(r for r in rows if r["id"] == release_id)
    return row if row["status"] not in ("releasing", "waiting") else None


async def test_the_key_is_sealed_and_checked(
    server: LiveServer, client: httpx.AsyncClient, play: PlayStandIn
) -> None:
    key_file = play.service_account(PACKAGE)
    shown = await save_key(client, key_file)
    assert shown["client_email"] == key_file["client_email"] and "private_key" not in shown
    async with server.services.db.session() as session:
        stored = (await session.scalars(select(PlayKey))).one()
    assert "PRIVATE KEY" not in stored.secret  # sealed with the vault
    checked = (await client.post("/api/me/play-key/check")).json()
    assert checked["check_ok"] and checked["notice"] == ""
    broken = await client.put("/api/me/play-key", json={
        "key_file": key_file | {"type": "authorized_user"}, "account": "organisation"})  # fmt: skip
    assert broken.status_code == 422
    personal = await save_key(client, key_file, "personal")
    assert "12 testers" in personal["notice"] and "14 days" in personal["notice"]


async def approvals_point_at(server: LiveServer, commit: str) -> None:
    async with server.services.db.session() as session, session.begin():
        await session.execute(update(GoLiveApproval).values(commit=commit))


async def test_nothing_is_released_without_the_approval(
    server: LiveServer, client: httpx.AsyncClient, play: PlayStandIn, host: HostStandIn
) -> None:
    played = play.add_app(PACKAGE)
    maria: Person = await person(server, "maria")
    async with host_online(server.url, await host_token(server, host), host):
        project_id, build_id = await built_app(server, maria.web)
    await save_key(maria.web, play.service_account(PACKAGE))
    await approvals_point_at(server, "0" * 40)  # the approval was of another commit
    unapproved = await release(maria.web, project_id, build_id)
    assert unapproved.status_code == 409 and "approved" in unapproved.text
    await approvals_point_at(server, (await maria.web.get(
        f"/api/projects/{project_id}/android/builds")).json()[0]["commit"])  # fmt: skip
    waiting = (await release(maria.web, project_id, build_id, notes="First")).json()
    assert waiting["status"] == "waiting" and play.state.bundles == []  # nothing went to Google
    approve = f"/api/admin/stores/play/{waiting['id']}/approve"
    assert (await maria.web.post(approve)).status_code == 403  # not an admin
    assert (await client.post(approve)).status_code == 200
    done = await until(lambda: settled(maria.web, project_id, waiting["id"]))
    assert done["status"] == "released", done["error"]
    assert played.tracks["internal"][0]["versionCodes"] == ["1"]
    certificate = (await maria.web.get(f"/api/projects/{project_id}/android/upload-key")).json()
    assert played.upload_sha256 == certificate["sha256"]  # Google now knows the upload key
    signed = await maria.web.get(f"/api/projects/{project_id}/android/builds/{build_id}/signed")
    assert signer_sha256(signed.content) == certificate["sha256"]
    with zipfile.ZipFile(io.BytesIO(signed.content)) as bundle:
        assert "base/manifest/AndroidManifest.xml" in bundle.namelist()


async def test_a_personal_account_is_told_about_the_closed_test(
    server: LiveServer, client: httpx.AsyncClient, play: PlayStandIn, host: HostStandIn
) -> None:
    played = play.add_app(PACKAGE)
    async with host_online(server.url, await host_token(server, host), host):
        project_id, build_id = await built_app(server, client)
    await save_key(client, play.service_account(PACKAGE, personal_new=True), "personal")
    refused = await release(client, project_id, build_id, track="production")
    assert refused.status_code == 409 and "12 testers" in refused.text
    claimed = (await release(client, project_id, build_id, track="production",
                             closed_test_done=True)).json()  # fmt: skip
    failed = await until(lambda: settled(client, project_id, claimed["id"]))
    assert failed["status"] == "failed" and "closed test" in failed["error"]
    assert "alpha" in failed["hint"]
    tested = (await release(client, project_id, build_id, track="alpha")).json()
    assert tested["admin"] == "admin"  # the dev user is an admin: no second approval
    done = await until(lambda: settled(client, project_id, tested["id"]))
    assert done["status"] == "released", done["error"]
    assert list(played.tracks) == ["alpha"]


async def test_a_first_release_stays_a_draft_and_refusals_say_what_to_do(
    server: LiveServer, client: httpx.AsyncClient, play: PlayStandIn, host: HostStandIn
) -> None:
    async with host_online(server.url, await host_token(server, host), host):
        project_id, build_id = await built_app(server, client)
    await save_key(client, play.service_account(PACKAGE))
    missing = (await release(client, project_id, build_id)).json()
    gone = await until(lambda: settled(client, project_id, missing["id"]))
    assert "Package not found" in gone["error"] and "Play Console" in gone["hint"]
    played = play.add_app(PACKAGE, published=False)
    first = (await release(client, project_id, build_id)).json()
    draft = await until(lambda: settled(client, project_id, first["id"]))
    assert draft["status"] == "draft" and "store listing" in draft["hint"]
    assert played.tracks["internal"][0]["status"] == "draft"
    played.upload_sha256 = "00:" * 31 + "00"  # Google knows another upload key
    second = (await release(client, project_id, build_id, track="beta")).json()
    wrong = await until(lambda: settled(client, project_id, second["id"]))
    assert "wrong key" in wrong["error"] and "upload key reset" in wrong["hint"]
