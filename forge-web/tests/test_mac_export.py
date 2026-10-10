"""Signing and exporting for the App Store on a Mac (W22): a keychain and profiles only for the
one export, cleaned up whatever happens; the signing material goes once to the Mac that runs
the job, and export jobs only to workers that know them."""

import asyncio
import base64
import io
import plistlib
import tarfile
from pathlib import Path
from typing import Any

import httpx
import pytest

from forge_macworker.client import WorkerClient
from forge_macworker.export import SIGNING, export_archive
from forge_macworker.job import ARCHIVE, JOB, SOURCE, run_job
from forge_macworker.runners import DirectRunner
from forge_macworker.wire import ExportParams, ExportResult, JobOffer, Profile, SigningMaterial
from forge_web.apple.jobs import NewJob
from test_apple_server import mac_header, world  # noqa: F401  (the server's Apple world)
from test_macworker import Builder

UUID = "6F9619FF-8B86-D011-B42D-00C04FC964FF"
PROFILE = Profile(name="Tally App Store", uuid=UUID, data_b64=base64.b64encode(b"profile").decode())
SIGNED = SigningMaterial(p12_b64=base64.b64encode(b"dist p12").decode(), password="p" * 32,
                         installer_p12_b64=base64.b64encode(b"installer p12").decode(),
                         profiles=[PROFILE])  # fmt: skip
IOS = ExportParams(platform="ios", team_id="TEAM123456",
                   profiles={"com.example.tally": "Tally App Store"})  # fmt: skip


def xcarchive_tgz() -> bytes:
    """A packed archive as the server keeps it."""
    data = io.BytesIO()
    with tarfile.open(fileobj=data, mode="w:gz") as packed:
        info = tarfile.TarInfo("Tally.xcarchive/Info.plist")
        info.size = 2
        packed.addfile(info, io.BytesIO(b"{}"))
    return data.getvalue()


class FakeMac:
    """Stands in for `security` and `xcodebuild`; remembers what was asked and seen."""

    def __init__(self, export_code: int = 0) -> None:
        self.calls: list[list[str]] = []
        self.export_code = export_code
        self.options: dict[str, Any] = {}
        self.profiles_seen: list[str] = []

    async def __call__(self, argv: list[str], cwd: Path) -> tuple[int, str]:
        self.calls.append(argv)
        if argv[:2] == ["security", "list-keychains"] and "-s" not in argv:
            return 0, '    "/Users/admin/Library/Keychains/login.keychain-db"\n'
        if argv[0] == "xcodebuild":
            self.options = plistlib.loads(Path(argv[argv.index("-exportOptionsPlist") + 1])
                                          .read_bytes())  # fmt: skip
            self.profiles_seen = [p.name for p in self.dirs[0].iterdir()]
            out = Path(argv[argv.index("-exportPath") + 1])
            if self.export_code == 0:
                out.mkdir(parents=True)
                suffix = ".pkg" if "installerSigningCertificate" in self.options else ".ipa"
                (out / f"Tally{suffix}").write_bytes(b"signed app")
                return 0, "** EXPORT SUCCEEDED **"
            return self.export_code, "error: No profile for team 'TEAM123456' matching ...\n"
        return 0, ""

    def named(self, *start: str) -> list[list[str]]:
        return [c for c in self.calls if c[: len(start)] == list(start)]


def job_folder(tmp_path: Path) -> Path:
    folder = tmp_path / "job"
    folder.mkdir()
    (folder / SOURCE).write_bytes(xcarchive_tgz())
    (folder / SIGNING).write_text(SIGNED.model_dump_json())
    return folder


async def test_an_export_signs_with_a_keychain_of_its_own_and_cleans_up(tmp_path: Path) -> None:
    folder, mac = job_folder(tmp_path), FakeMac()
    mac.dirs = (tmp_path / "profiles-a", tmp_path / "profiles-b")  # type: ignore[attr-defined]
    result = await export_archive(IOS, folder, folder / SOURCE, folder / ARCHIVE, run=mac,
                                  profile_dirs=mac.dirs)  # type: ignore[attr-defined]  # fmt: skip
    assert result.ok and result.file_name == "Tally.ipa" and result.size == len(b"signed app")
    with tarfile.open(folder / ARCHIVE) as packed:
        assert packed.getnames() == ["Tally.ipa"]
    assert mac.options["method"] == "app-store-connect" and mac.options["signingStyle"] == "manual"
    assert mac.options["teamID"] == "TEAM123456" and mac.options["destination"] == "export"
    assert mac.options["provisioningProfiles"] == {"com.example.tally": "Tally App Store"}
    assert mac.profiles_seen == [f"{UUID}.mobileprovision"]  # there while exporting ...
    assert not any(d.exists() and any(d.iterdir()) for d in mac.dirs)  # ... and gone after
    [created] = mac.named("security", "create-keychain")
    keychain = created[-1]
    assert [c[-1] for c in mac.named("security", "delete-keychain")] == [keychain]
    searched = mac.named("security", "list-keychains", "-d", "user", "-s")
    assert searched[0][5] == keychain and searched[-1][5:] == [
        "/Users/admin/Library/Keychains/login.keychain-db"  # the user's list is put back
    ]
    assert len(mac.named("security", "import")) == 2  # distribution and installer
    assert not (folder / SIGNING).exists() and not (folder / "export").exists()


async def test_a_failed_export_says_why_and_still_cleans_up(tmp_path: Path) -> None:
    folder, mac = job_folder(tmp_path), FakeMac(export_code=70)
    mac.dirs = (tmp_path / "profiles",)  # type: ignore[attr-defined]
    result = await export_archive(IOS, folder, folder / SOURCE, folder / ARCHIVE, run=mac,
                                  profile_dirs=mac.dirs)  # type: ignore[attr-defined]  # fmt: skip
    assert not result.ok and "No profile for team" in result.log_tail
    assert not (folder / ARCHIVE).exists()
    assert mac.named("security", "delete-keychain") and not any(mac.dirs[0].iterdir())  # type: ignore[attr-defined]


async def test_a_mac_export_is_a_signed_installer(tmp_path: Path) -> None:
    folder, mac = job_folder(tmp_path), FakeMac()
    mac.dirs = (tmp_path / "profiles",)  # type: ignore[attr-defined]
    params = IOS.model_copy(update={"platform": "macos"})
    result = await export_archive(params, folder, folder / SOURCE, folder / ARCHIVE, run=mac,
                                  profile_dirs=mac.dirs)  # type: ignore[attr-defined]  # fmt: skip
    assert result.ok and result.file_name == "Tally.pkg"
    assert mac.options["installerSigningCertificate"] == "3rd Party Mac Developer Installer"


async def test_a_release_archive_job_asks_for_the_build_number(tmp_path: Path) -> None:
    calls: list[tuple[Any, ...]] = []

    class Releasing(Builder):
        async def release_archive(self, platform: str, number: int, scheme: str | None) -> Any:
            calls.append((platform, number))
            return await self.build(platform, "archive", scheme)  # type: ignore[arg-type]

    folder = tmp_path / "job"
    folder.mkdir()
    offer = {"id": "1" * 32, "kind": "build", "project": "ab" * 16, "timeout_s": 60,
             "build": {"platform": "ios", "action": "archive",
                       "build_number": 29012345}}  # fmt: skip
    (folder / JOB).write_text(JobOffer.model_validate(offer).model_dump_json())
    data = io.BytesIO()
    with tarfile.open(fileobj=data, mode="w:gz") as packed:
        info = tarfile.TarInfo("Shared/App.swift")
        packed.addfile(info, io.BytesIO(b""))
    (folder / SOURCE).write_bytes(data.getvalue())
    result = await run_job(folder, tmp_path / "work", Releasing)
    assert result.ok and calls == [("ios", 29012345)] and (folder / ARCHIVE).is_file()


async def test_an_export_job_from_the_server_to_a_mac_and_back(
    world: Any,  # noqa: F811
    tmp_path: Path,
) -> None:
    seen: list[SigningMaterial] = []

    async def exporter(params: ExportParams, folder: Path, archive: Path, target: Path) -> Any:
        seen.append(SigningMaterial.model_validate_json((folder / SIGNING).read_text()))
        assert (folder / SIGNING).stat().st_mode & 0o077 == 0  # only the worker may read it
        target.write_bytes(b"the signed ipa, packed")
        return ExportResult(ok=True, platform="ios", file_name="Tally.ipa", size=10)

    source = tmp_path / "archive.tar.gz"
    source.write_bytes(xcarchive_tgz())
    job = NewJob("p1", "c1", "u1", IOS, signing=SIGNED)
    waiting = asyncio.create_task(world.jobs.run(job, source))
    await asyncio.sleep(0.1)
    old = await world.mac.post("/api/mac/poll", json={"free_slots": 1, "version": "0.1.0"},
                               headers=mac_header(world.token))  # fmt: skip
    assert old.json()["job"] is None  # an old worker cannot sign: it gets no export jobs
    transport = httpx.ASGITransport(app=world.mac._transport.app)
    runner = DirectRunner(tmp_path / "mac", Builder, exporter)
    client = WorkerClient("http://srv", world.token, runner, slots=1, transport=transport)
    stop = asyncio.Event()
    serving = asyncio.create_task(client.serve(stop))
    result = await asyncio.wait_for(waiting, 30)
    stop.set()
    await asyncio.wait_for(serving, 5)
    assert result.ok and result.export is not None and result.export.artifact.startswith("job:")
    job_id = result.export.artifact[4:]
    assert (
        world.root / "apple" / job_id / "archive.tar.gz"
    ).read_bytes() == b"the signed ipa, packed"
    assert seen == [SIGNED]
    again = await world.mac.get(f"/api/mac/jobs/{job_id}/signing", headers=mac_header(world.token))
    assert again.status_code == 409  # handed out once, and the job is over
    assert world.jobs.signing == {}


def test_signing_material_is_never_part_of_an_offer() -> None:
    fields = set(JobOffer.model_fields) | set(ExportParams.model_fields)
    assert not fields & {"p12_b64", "password", "installer_p12_b64", "private_key"}
    with pytest.raises(ValueError):
        JobOffer.model_validate({"id": "2" * 32, "kind": "export", "project": "ab" * 16,
                                 "timeout_s": 60})  # fmt: skip
