"""The parts of a release to TestFlight (W22b): what an archive holds, what an export sent, where
uploads may go, build numbers, and certificates and profiles made through the stand-in for
App Store Connect."""

import base64
import io
import plistlib
import tarfile
import time
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.serialization import pkcs12
from sqlalchemy import select

from asc_standin import ISSUER_ID, KEY_ID, TEAM_ID, AscStandIn
from forge_web.apple.archive_info import ArchiveProblem, archive_info, product_of
from forge_web.apple.asc_client import AscClient, AscKey
from forge_web.apple.release import build_number_after
from forge_web.apple.signing import (
    DISTRIBUTION,
    INSTALLER,
    Owner,
    SigningProblem,
    certificate,
    signing_for,
)
from forge_web.apple.upload import upload_url_allowed
from forge_web.db.engine import Database
from forge_web.db.models import AppleCertificate, User
from forge_web.vault import Vault


def packed(files: dict[str, bytes]) -> bytes:
    """A tar.gz with these files."""
    data = io.BytesIO()
    with tarfile.open(fileobj=data, mode="w:gz") as tar:
        for name, content in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(content)
            tar.addfile(info, io.BytesIO(content))
    return data.getvalue()


def plist(identifier: str, version: str = "1.0", build: str = "7") -> bytes:
    return plistlib.dumps({"CFBundleIdentifier": identifier, "CFBundleShortVersionString": version,
                           "CFBundleVersion": build}, fmt=plistlib.FMT_BINARY)  # fmt: skip


def test_build_numbers_only_go_up() -> None:
    now = 1_760_000_000.0
    assert build_number_after(0, now) == int(now // 60)
    assert build_number_after(int(now // 60), now) == int(now // 60) + 1  # twice in a minute


def test_an_archive_says_which_bundles_to_sign(tmp_path: Path) -> None:
    app = "Tally.xcarchive/Products/Applications/Tally.app"
    ios = tmp_path / "ios.tar.gz"
    ios.write_bytes(packed({
        "Tally.xcarchive/Info.plist": plistlib.dumps({"Name": "Tally"}),
        f"{app}/Watch/TallyWatch.app/Info.plist": plist("com.example.tally.watchkitapp"),
        f"{app}/Info.plist": plist("com.example.tally"),
        f"{app}/Frameworks/Kit.framework/Info.plist": plist("com.example.kit"),
        "Tally.xcarchive/dSYMs/Tally.app.dSYM/Contents/Info.plist": plist("com.apple.xcode.dsym"),
    }))  # fmt: skip
    info = archive_info(ios)
    assert info.bundle_id == "com.example.tally" and (info.version, info.build) == ("1.0", "7")
    assert info.bundles == ("com.example.tally", "com.example.tally.watchkitapp")
    mac = tmp_path / "mac.tar.gz"
    mac.write_bytes(packed({
        f"{app}/Contents/Info.plist": plist("com.example.tally", build="8"),
        f"{app}/Contents/PlugIns/Widget.appex/Contents/Info.plist": plist("com.example.tally.w"),
    }))  # fmt: skip
    assert archive_info(mac).bundles == ("com.example.tally", "com.example.tally.w")
    empty = tmp_path / "empty.tar.gz"
    empty.write_bytes(packed({"Tally.xcarchive/Info.plist": plistlib.dumps({})}))
    with pytest.raises(ArchiveProblem):
        archive_info(empty)


def test_an_export_must_be_exactly_one_product(tmp_path: Path) -> None:
    good = tmp_path / "good.tar.gz"
    good.write_bytes(packed({"Tally.ipa": b"signed"}))
    assert product_of(good, "ios", tmp_path / "out").read_bytes() == b"signed"
    for files, platform in (({"Tally.ipa": b"a", "more": b"b"}, "ios"),
                            ({"../Tally.ipa": b"a"}, "ios"), ({"Tally.ipa": b"a"}, "macos"),
                            ({"dir/Tally.pkg": b"a"}, "macos")):  # fmt: skip
        bad = tmp_path / "bad.tar.gz"
        bad.write_bytes(packed(files))
        with pytest.raises(ArchiveProblem):
            product_of(bad, platform, tmp_path / "bad-out")


def test_uploads_go_only_to_apple() -> None:
    apple = "https://api.appstoreconnect.apple.com"
    assert upload_url_allowed("https://store-032.blobstore.apple.com/x?sig=1", apple)
    assert not upload_url_allowed("http://store-032.blobstore.apple.com/x", apple)
    assert not upload_url_allowed("https://apple.com.evil.example/x", apple)
    assert not upload_url_allowed("http://127.0.0.1:9/upload", apple)
    stand_in = "http://127.0.0.1:4321"
    assert upload_url_allowed("http://127.0.0.1:4321/upload/a/0", stand_in)
    assert not upload_url_allowed("http://127.0.0.1:9999/upload/a/0", stand_in)


@pytest.fixture
def asc() -> Iterator[AscStandIn]:
    with AscStandIn() as standin:
        yield standin


@pytest.fixture
async def owner(tmp_path: Path) -> AsyncIterator[Owner]:
    db = Database(f"sqlite+aiosqlite:///{tmp_path / 'a.db'}")
    await db.migrate()
    async with db.session() as session, session.begin():
        session.add(User(id="u1", email="a@x", role="member", status="active",
                         created_at=time.time()))  # fmt: skip
    yield Owner(db, Vault(b"m" * 32), "u1", TEAM_ID)
    await db.close()


async def stored(owner: Owner) -> list[AppleCertificate]:
    async with owner.db.session() as session:
        return list(await session.scalars(select(AppleCertificate)))


async def test_certificates_are_made_once_and_kept_encrypted(asc: AscStandIn, owner: Owner) -> None:
    client = AscClient(asc.url, AscKey(KEY_ID, ISSUER_ID, TEAM_ID, asc.pem))
    first = await certificate(owner, client, DISTRIBUTION)
    again = await certificate(owner, client, DISTRIBUTION)
    assert first == again and len(asc.state.release.certificates) == 1
    [row] = await stored(owner)
    assert "PRIVATE KEY" not in row.secret and "PRIVATE KEY" in owner.vault.decrypt(row.secret)
    asc.state.release.certificates[first.asc_id]["revoked"] = True  # revoked by the team
    renewed = await certificate(owner, client, DISTRIBUTION)
    assert renewed.asc_id != first.asc_id and [r.id for r in await stored(owner)] == [
        renewed.asc_id
    ]
    await client.close()


async def test_a_team_with_too_many_certificates_is_told_what_to_do(
    asc: AscStandIn, owner: Owner
) -> None:
    client = AscClient(asc.url, AscKey(KEY_ID, ISSUER_ID, TEAM_ID, asc.pem))
    for team in ("ELSEWHERE1", "ELSEWHERE2"):  # the team's limit, used up by others
        await certificate(Owner(owner.db, owner.vault, "u1", team), client, DISTRIBUTION)
    with pytest.raises(SigningProblem) as refused:
        await certificate(owner, client, DISTRIBUTION)
    assert "Distribution certificate" in str(refused.value) and "revoke" in refused.value.hint
    await client.close()


async def test_an_export_gets_profiles_for_every_bundle_and_p12_files(
    asc: AscStandIn, owner: Owner
) -> None:
    client = AscClient(asc.url, AscKey(KEY_ID, ISSUER_ID, TEAM_ID, asc.pem))
    bundles = ("com.example.tally", "com.example.tally.watchkitapp")
    params, material = await signing_for(owner, client, "ios", bundles)
    assert params.team_id == TEAM_ID and set(params.profiles) == set(bundles)
    assert [p.name for p in material.profiles] == list(params.profiles.values())
    registered = {b["attributes"]["identifier"]: b["attributes"]["platform"]
                  for b in asc.state.bundle_ids}  # fmt: skip
    assert registered == dict.fromkeys(bundles, "IOS")
    key, cert, _ = pkcs12.load_key_and_certificates(base64.b64decode(material.p12_b64),
                                                    material.password.encode())  # fmt: skip
    assert (
        key is not None
        and cert is not None
        and "Apple Distribution" in cert.subject.rfc4514_string()
    )
    assert material.installer_p12_b64 == ""  # only the Mac needs the installer certificate
    _, again = await signing_for(owner, client, "ios", bundles)  # profiles anew, same names
    assert len(asc.state.release.profiles) == 2 and again.password != material.password
    mac_params, mac = await signing_for(owner, client, "macos", bundles[:1])
    _, installer, _ = pkcs12.load_key_and_certificates(base64.b64decode(mac.installer_p12_b64),
                                                       mac.password.encode())  # fmt: skip
    assert installer is not None and "Installer" in installer.subject.rfc4514_string()
    kinds = sorted(
        c["attributes"]["certificateType"] for c in asc.state.release.certificates.values()
    )
    assert kinds == [DISTRIBUTION, INSTALLER] and mac_params.platform == "macos"
    await client.close()
