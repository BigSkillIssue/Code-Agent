"""A release to TestFlight (W22b), end to end: from the user's approval of a commit through the
Mac (archive, then signing in a VM of its own) to App Store Connect (upload, processing,
TestFlight), against a stand-in Mac and the stand-in for Apple's API. It also goes on after a
restart and starts again where it failed."""

import asyncio
import base64
import contextlib
import io
import plistlib
import subprocess
import tarfile
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from cryptography.hazmat.primitives.serialization import pkcs12
from forge.ports import AppleAction, AppleBuildResult, ApplePlatform, AppleScreen
from forge.providers.base import ImagePart
from sqlalchemy import select

from asc_standin import ISSUER_ID, KEY_ID, TEAM_ID, AscStandIn
from asc_standin_store import png
from forge_macworker.client import WorkerClient
from forge_macworker.export import SIGNING
from forge_macworker.runners import DirectRunner
from forge_macworker.wire import ExportParams, ExportResult, SigningMaterial
from forge_web.db.models import AppleApproval, AuditEntry
from support import LiveServer, dev_settings
from test_store_media import fitter

BUNDLE = "com.example.tally"


class Mac:
    """Stands in for Xcode and the export on a Mac; remembers what it was given."""

    sources: list[set[str]] = []
    exports: list[tuple[ExportParams, SigningMaterial]] = []

    def __init__(self, root: Path, data: Path) -> None:
        self.root, self.data = root, data

    async def build(
        self, platform: ApplePlatform, action: AppleAction, scheme: str | None = None
    ) -> AppleBuildResult:
        return AppleBuildResult(ok=True, platform=platform, action=action, scheme="Tally")

    async def release_archive(
        self, platform: ApplePlatform, number: int, scheme: str | None = None
    ) -> AppleBuildResult:
        Mac.sources.append({p.relative_to(self.root).as_posix() for p in self.root.rglob("*")})
        archive = self.data / f"Tally-{platform}-{number}.xcarchive"  # as Xcode's are named
        app = archive / "Products" / "Applications" / "Tally.app"
        bundles = (
            {app / "Contents": BUNDLE}
            if platform == "macos"
            else {app: BUNDLE, app / "Watch" / "TallyWatch.app": f"{BUNDLE}.watchkitapp"}
        )
        for folder, identifier in bundles.items():
            folder.mkdir(parents=True, exist_ok=True)
            (folder / "Info.plist").write_bytes(plistlib.dumps({
                "CFBundleIdentifier": identifier, "CFBundleShortVersionString": "1.0",
                "CFBundleVersion": str(number)}))  # fmt: skip
        return AppleBuildResult(ok=True, platform=platform, action="archive", scheme="Tally",
                                artifact=str(archive))  # fmt: skip

    async def screenshot(
        self, platform: ApplePlatform, device: str | None = None, dark: bool = False
    ) -> AppleScreen:
        image = ImagePart(media_type="image/png", data_b64=base64.b64encode(png(300, 600)).decode())
        return AppleScreen(platform=platform, device=device or platform, dark=dark, image=image)

    async def close(self) -> None:
        """Nothing to stop."""


async def export(params: ExportParams, folder: Path, archive: Path, target: Path) -> ExportResult:
    """The export: the signing material must be there, whole; the product goes back packed."""
    material = SigningMaterial.model_validate_json((folder / SIGNING).read_text())
    (folder / SIGNING).unlink()
    pkcs12.load_key_and_certificates(base64.b64decode(material.p12_b64), material.password.encode())
    Mac.exports.append((params, material))
    name = "Tally.pkg" if params.platform == "macos" else "Tally.ipa"
    product = f"signed {params.platform} app ".encode() * 40
    with tarfile.open(target, "w:gz") as packed:
        info = tarfile.TarInfo(name)
        info.size = len(product)
        packed.addfile(info, io.BytesIO(product))
    return ExportResult(ok=True, platform=params.platform, file_name=name, size=len(product))


@pytest.fixture
def asc() -> Iterator[AscStandIn]:
    with AscStandIn() as standin:
        standin.state.release.part_size = 100  # several parts per file
        yield standin


def settings_for(data: Path, asc: AscStandIn) -> Any:
    settings = dev_settings(data)
    settings.apple.enabled, settings.apple.allowed = True, "everyone"
    settings.apple.asc_api_url, settings.apple.release_poll_s = asc.url, 0.05
    settings.apple.review_poll_s = 0.05
    return settings


@pytest.fixture
def server(tmp_path: Path, asc: AscStandIn) -> Iterator[LiveServer]:
    with LiveServer(settings_for(tmp_path / "data", asc)) as live:
        yield live


@pytest.fixture
async def client(server: LiveServer) -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(base_url=server.url, headers=server.headers(), timeout=60) as c:
        yield c


@contextlib.asynccontextmanager
async def mac_online(client: httpx.AsyncClient, url: str, work: Path) -> AsyncIterator[None]:
    """A stand-in Mac connected to the server while the block runs."""
    Mac.sources, Mac.exports = [], []
    token = (await client.post("/api/admin/apple/macs", json={"name": "mac"})).json()["token"]
    worker = WorkerClient(url, token, DirectRunner(work, Mac, export, fitter))
    stop = asyncio.Event()
    serving = asyncio.create_task(worker.serve(stop))
    try:
        yield
    finally:
        stop.set()
        with contextlib.suppress(asyncio.CancelledError, TimeoutError):
            await asyncio.wait_for(serving, 10)


async def until(check: Callable[[], Awaitable[Any]], timeout: float = 60) -> Any:
    async with asyncio.timeout(timeout):
        while not (found := await check()):
            await asyncio.sleep(0.1)
        return found


async def approved_project(server: LiveServer, client: httpx.AsyncClient, asc: AscStandIn) -> str:
    """An Apple project with the user's key and an approval of its commit."""
    project = (await client.post("/api/projects", json={"name": "Tally", "source": "apple"})).json()
    workspace = server.services.driver.workspace(project["id"])
    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=workspace, capture_output=True,
                          text=True, check=True).stdout.strip()  # fmt: skip
    key = {"key_id": KEY_ID, "issuer_id": ISSUER_ID, "team_id": TEAM_ID, "private_key": asc.pem}
    assert (await client.put("/api/me/appstore-key", json=key)).status_code == 200
    async with server.services.db.session() as session, session.begin():
        session.add(AppleApproval(id="a1", project_id=project["id"], chat_id="c1",
                                  user_id=server.services.dev_user_id, request_id="r1",
                                  commit=head, clean=True, created_at=time.time()))  # fmt: skip
    return str(project["id"])


async def finished(client: httpx.AsyncClient, project_id: str, count: int) -> list[Any] | None:
    releases = (await client.get(f"/api/projects/{project_id}/apple/releases")).json()
    if len(releases) == count and all(r["status"] != "running" for r in releases):
        return list(releases)
    return None


async def test_an_approved_commit_goes_to_testflight(
    server: LiveServer, client: httpx.AsyncClient, asc: AscStandIn, tmp_path: Path
) -> None:
    asc.add_app("Tally", BUNDLE)
    project_id = await approved_project(server, client, asc)
    workspace = server.services.driver.workspace(project_id)
    (workspace / "build").mkdir()
    (workspace / "build" / "left-over.txt").write_text("not committed")  # ignored by git
    async with mac_online(client, server.url, tmp_path / "mac"):
        started = await client.post(f"/api/projects/{project_id}/apple/releases",
                                    json={"platforms": ["ios", "macos"]})  # fmt: skip
        assert started.status_code == 200, started.text
        releases = await until(lambda: finished(client, project_id, 2), 90)
    assert [(r["status"], r["step"]) for r in releases] == [("done", "done")] * 2, [
        r["error"] for r in releases
    ]
    assert {r["platform"] for r in releases} == {"ios", "macos"}
    assert all("project.yml" in files and "build/left-over.txt" not in files
               for files in Mac.sources)  # exactly the commit, nothing else  # fmt: skip
    profiles = {p.platform: set(p.profiles) for p, _ in Mac.exports}
    assert profiles == {"ios": {BUNDLE, f"{BUNDLE}.watchkitapp"}, "macos": {BUNDLE}}
    release = asc.state.release
    uploads = sorted((u["attributes"]["platform"], u["data"]) for u in release.uploads.values())
    assert [platform for platform, _ in uploads] == ["IOS", "MAC_OS"]
    assert uploads[0][1] == b"signed ios app " * 40  # whole, in parts, checksum matched
    assert release.credentials_at_upload == 0  # Apple's upload URLs never get the API token
    kinds = sorted(c["attributes"]["certificateType"] for c in release.certificates.values())
    assert kinds == ["DISTRIBUTION", "MAC_INSTALLER_DISTRIBUTION"]  # one each, for both releases
    assert all(b["attributes"]["processingState"] == "VALID" for b in release.builds.values())
    [group] = release.beta_groups
    assert group["attributes"] == {"name": "Forge", "isInternalGroup": True,
                                   "hasAccessToAllBuilds": True}  # fmt: skip
    assert server.services.apple.signing == {}  # handed out and gone
    async with server.services.db.session() as session:
        actions = [a.action for a in await session.scalars(select(AuditEntry))]
    assert "apple.release_started" in actions


async def test_a_release_needs_a_key_an_approval_and_the_approved_commit(
    server: LiveServer, client: httpx.AsyncClient, asc: AscStandIn
) -> None:
    project = (await client.post("/api/projects", json={"name": "Tally", "source": "apple"})).json()
    url = f"/api/projects/{project['id']}/apple/releases"
    body = {"platforms": ["ios"]}
    no_key = await client.post(url, json=body)
    assert no_key.status_code == 409 and "App Store Connect key" in no_key.text
    project_id = await approved_project(server, client, asc)
    other_url = f"/api/projects/{project['id']}/apple/releases"
    unapproved = await client.post(other_url, json=body)  # the first project has no approval
    assert unapproved.status_code == 409 and "no approval" in unapproved.text
    workspace = server.services.driver.workspace(project_id)
    (workspace / "Shared" / "Later.swift").write_text("// after the approval\n")
    changed = await client.post(f"/api/projects/{project_id}/apple/releases", json=body)
    assert changed.status_code == 409 and "changed since it was approved" in changed.text
    bad = await client.post(f"/api/projects/{project_id}/apple/releases", json={"platforms": []})
    assert bad.status_code == 422
    assert (await client.get(f"/api/projects/{project_id}/apple/releases")).json() == []


async def test_a_failed_release_starts_again_where_it_failed(
    server: LiveServer, client: httpx.AsyncClient, asc: AscStandIn, tmp_path: Path
) -> None:
    project_id = await approved_project(server, client, asc)
    url = f"/api/projects/{project_id}/apple/releases"
    async with mac_online(client, server.url, tmp_path / "mac"):
        await client.post(url, json={"platforms": ["ios"]})
        [failed] = await until(lambda: finished(client, project_id, 1))
        assert (failed["status"], failed["step"]) == ("failed", "identify")
        assert BUNDLE in failed["error"] and "App Store Connect" in failed["hint"]
        registered = {b["attributes"]["identifier"] for b in asc.state.bundle_ids}
        assert BUNDLE in registered  # so the user can pick it for the new app record
        asc.add_app("Tally", BUNDLE)  # the user makes the app record
        again = await client.post(f"{url}/{failed['id']}/retry")
        assert again.status_code == 200, again.text
        [done] = await until(lambda: finished(client, project_id, 1))
    assert done["status"] == "done" and done["build_number"] == failed["build_number"]
    assert len(Mac.sources) == 1  # the archive from before was used again
    twice = await client.post(f"{url}/{failed['id']}/retry")
    assert twice.status_code == 409


async def test_a_release_goes_on_after_a_restart(tmp_path: Path, asc: AscStandIn) -> None:
    asc.add_app("Tally", BUNDLE)
    asc.state.release.polls = 10**9  # Apple takes its time
    settings = settings_for(tmp_path / "data", asc)
    with LiveServer(settings) as first:
        async with httpx.AsyncClient(base_url=first.url, headers=first.headers(),
                                     timeout=60) as client:  # fmt: skip
            project_id = await approved_project(first, client, asc)
            async with mac_online(client, first.url, tmp_path / "mac"):
                await client.post(f"/api/projects/{project_id}/apple/releases",
                                  json={"platforms": ["ios"]})  # fmt: skip

                async def processing() -> bool:
                    [r] = (await client.get(f"/api/projects/{project_id}/apple/releases")).json()
                    return bool(r["step"] == "process")

                await until(processing, 90)
    asc.state.release.polls = 1  # Apple is done while the server was away
    with LiveServer(settings) as second:
        async with httpx.AsyncClient(base_url=second.url, headers=second.headers(),
                                     timeout=60) as client:  # fmt: skip
            [done] = await until(lambda: finished(client, project_id, 1))
    assert (done["status"], done["step"]) == ("done", "done")
    assert len(asc.state.release.uploads) == 1  # not uploaded twice
