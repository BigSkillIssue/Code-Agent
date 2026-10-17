"""Android builds (W37a): the newest approved commit is built on the app's host in a throwaway
container without secrets, on the build network only, and its unsigned bundle comes back to
Forge Web; every build has a higher version code; a missing app, image, approval or host, and a
failed build, say why. The host is the stand-in (tests/host_standin.py), its Docker the fake
one (tests/docker_standin.py)."""

import asyncio
import shutil
import zipfile
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from pydantic import ValidationError
from sqlalchemy import delete

from docker_standin import FakeDocker, runner_for, tarball
from forge_hostworker.android import bundle_path
from forge_hostworker.wire import WITH_SECRETS, AndroidBuild, HostJob, JobResult
from forge_web.db.hosting_models import HostedApp
from forge_web.stores.android import android_app, is_bundle
from host_standin import HostStandIn, host_online, stand_in_bundle
from support import LiveServer, free_port
from test_hosting_deploy import (
    app_project,
    approve_head,
    deployed,
    git,
    host_token,
    settings_for,
    settled,
    short_polls,
    until,
)

__all__ = ["short_polls"]
IMAGE = "runtime/android:35"
GRADLE = b'android {\n    defaultConfig {\n        applicationId = "com.example.shop"\n    }\n}\n'
ANDROID = {"android/gradlew": b"#!/bin/sh\n", "android/app/build.gradle.kts": GRADLE}


class GradleDocker(FakeDocker):
    """Docker whose Gradle build leaves a release bundle in the folder it was given."""

    def do_run(self, args: list[str]) -> Any:
        result = super().do_run(args)
        if result.code == 0 and "bundleRelease" in args:
            folder = Path(args[args.index("-v") + 1].removesuffix(":/app"))
            bundle = folder / "app/build/outputs/bundle/release/app-release.aab"
            bundle.parent.mkdir(parents=True, exist_ok=True)
            bundle.write_bytes(b"PK\x03\x04 a release bundle")
        return result


class LinkingDocker(FakeDocker):
    """Docker whose Gradle build leaves a link to a file of the host where the bundle goes, or
    a linked folder on the way to it."""

    def __init__(self, secret: Path, folder_link: bool = False) -> None:
        super().__init__()
        self.secret, self.folder_link = secret, folder_link

    def do_run(self, args: list[str]) -> Any:
        result = super().do_run(args)
        if result.code == 0 and "bundleRelease" in args:
            folder = Path(args[args.index("-v") + 1].removesuffix(":/app"))
            if self.folder_link:
                (folder / "app").mkdir(parents=True, exist_ok=True)
                elsewhere = self.secret.parent / "outputs"
                (elsewhere / "bundle/release").mkdir(parents=True, exist_ok=True)
                (elsewhere / "bundle/release/app-release.aab").write_bytes(b"PK\x03\x04 x")
                (folder / "app/build").mkdir()
                (folder / "app/build/outputs").symlink_to(elsewhere)
            else:
                bundle = folder / "app/build/outputs/bundle/release/app-release.aab"
                bundle.parent.mkdir(parents=True, exist_ok=True)
                bundle.symlink_to(self.secret)
        return result


def android_job(code: int = 7) -> HostJob:
    build = AndroidBuild(image=IMAGE, version_code=code, version_name=f"1.0.{code}")
    return HostJob(id="b" * 32, kind="android", app="shop", environment="staging", android=build)


async def test_the_build_runs_in_a_throwaway_container_without_secrets(tmp_path: Path) -> None:
    docker = GradleDocker()
    runner = runner_for(tmp_path, docker)
    source = tarball(tmp_path / "source.tar.gz", ANDROID)
    result = await runner.run(android_job(), source, {})
    assert result.ok, result.error
    [run] = docker.runs()
    assert run[:2] == ["run", "--rm"] and "--runtime=runsc" in run and "--read-only" in run
    assert run[run.index("--cap-drop") + 1] == "ALL"
    assert run[run.index("--network") + 1] == "forge-build"  # Maven and Google, nothing else
    assert "--env-file" not in run
    assert [run[i + 1] for i, a in enumerate(run) if a == "-e"] == [
        "HOME=/tmp",
        "GRADLE_USER_HOME=/tmp/gradle",
    ]
    assert run[run.index(IMAGE) + 1 :] == [
        "./gradlew", "--no-daemon", "-PversionCode=7", "-PversionName=1.0.7",
        "testReleaseUnitTest", "bundleRelease",
    ]  # fmt: skip
    assert "android" not in WITH_SECRETS  # the client never asks for any
    assert bundle_path(runner.data_dir, "b" * 32).read_bytes().startswith(b"PK")
    assert not (runner.data_dir / "android" / ("b" * 32)).exists()  # nothing else stays


async def test_a_link_instead_of_the_bundle_sends_nothing_of_the_host(tmp_path: Path) -> None:
    secret = tmp_path / "host" / "host.key"
    secret.parent.mkdir()
    secret.write_text("the host's private key", encoding="utf-8")
    for folder_link in (False, True):
        runner = runner_for(tmp_path / f"r{folder_link}", LinkingDocker(secret, folder_link))
        source = tarball(tmp_path / f"s{folder_link}.tar.gz", ANDROID)
        result = await runner.run(android_job(), source, {})
        assert not result.ok and result.error == "the build made no release bundle"
        assert not bundle_path(runner.data_dir, "b" * 32).exists()
    assert secret.read_text(encoding="utf-8") == "the host's private key"


async def test_a_missing_app_or_a_failed_build_says_why(tmp_path: Path) -> None:
    docker = GradleDocker()
    runner = runner_for(tmp_path, docker)
    plain = await runner.run(android_job(), tarball(tmp_path / "plain.tar.gz"), {})
    assert not plain.ok and "android/gradlew" in plain.error and "--android" in plain.hint
    assert docker.runs() == []
    docker.fail.add(IMAGE)
    broken = await runner.run(android_job(), tarball(tmp_path / "broken.tar.gz", ANDROID), {})
    assert not broken.ok and broken.error == "the Android build or its tests failed"
    assert "build failed" in broken.log_tail
    empty = FakeDocker()  # the build "works" but leaves no bundle
    none = await runner_for(tmp_path / "e", empty).run(
        android_job(), tarball(tmp_path / "none.tar.gz", ANDROID), {}
    )
    assert not none.ok and none.error == "the build made no release bundle"


def test_an_android_job_says_what_to_build_with() -> None:
    with pytest.raises(ValidationError):
        HostJob(id="b" * 32, kind="android", app="shop", environment="staging")
    with pytest.raises(ValidationError):
        HostJob(id="b" * 32, kind="check", app="shop", environment="staging",
                android=android_job().android)  # fmt: skip
    with pytest.raises(ValidationError):
        AndroidBuild(image=IMAGE, version_code=0, version_name="1.0")
    with pytest.raises(ValidationError):
        AndroidBuild(image=IMAGE, version_code=1, version_name="1.0; rm -rf /")


def test_the_application_id_comes_from_the_gradle_file(tmp_path: Path) -> None:
    assert android_app(tarball(tmp_path / "web.tar.gz")) is None
    assert android_app(tarball(tmp_path / "app.tar.gz", ANDROID)) == "com.example.shop"
    unnamed = {"android/gradlew": b"#!/bin/sh\n", "android/app/build.gradle.kts": b"android {}\n"}
    assert android_app(tarball(tmp_path / "unnamed.tar.gz", unnamed)) == ""


def server_settings(data: Path) -> Any:
    settings = settings_for(data)
    settings.hosting.android_image = IMAGE
    return settings


@pytest.fixture
def server(tmp_path: Path) -> Iterator[LiveServer]:
    with LiveServer(server_settings(tmp_path / "data")) as live:
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


async def android_project(server: LiveServer, client: httpx.AsyncClient) -> str:
    """An app project with an Android app, live in staging (so it is on a host)."""
    project_id, _ = await app_project(server, client, server.services.dev_user_id)
    staging = await deployed(client, project_id, "staging")
    assert (await until(lambda: settled(client, project_id, staging)))["status"] == "live"
    await add_android(server, project_id)
    return project_id


async def add_android(server: LiveServer, project_id: str) -> None:
    workspace = server.services.driver.workspace(project_id)
    for name, data in ANDROID.items():
        (workspace / name).parent.mkdir(parents=True, exist_ok=True)
        (workspace / name).write_bytes(data)
    git(workspace, "add", "-A")
    git(workspace, "commit", "-qm", "the Android app")
    await approve_head(server, project_id, server.services.dev_user_id)


async def started(client: httpx.AsyncClient, project_id: str) -> dict[str, Any]:
    answer = await client.post(f"/api/projects/{project_id}/android/builds")
    assert answer.status_code == 200, answer.text
    return dict(answer.json())


async def finished(client: httpx.AsyncClient, project_id: str, build_id: str) -> Any:
    rows = (await client.get(f"/api/projects/{project_id}/android/builds")).json()
    row = next(r for r in rows if r["id"] == build_id)
    return row if row["status"] != "building" else None


async def test_an_approved_commit_becomes_a_bundle_with_a_new_version_code(
    server: LiveServer, client: httpx.AsyncClient, host: HostStandIn
) -> None:
    async with host_online(server.url, await host_token(server, host), host):
        project_id = await android_project(server, client)
        first = await started(client, project_id)
        done = await until(lambda: finished(client, project_id, first["id"]))
        assert done["status"] == "built", done["error"]
        assert (done["version_code"], done["version_name"]) == (1, "1.0.1")
        assert done["package_name"] == "com.example.shop"  # what Google Play knows it by
        job = host.last("android")
        assert job.android is not None and job.android.image == IMAGE
        assert "android/gradlew" in host.sources[job.id]
        assert job.id not in host.secrets  # the build never sees a secret
        url = f"/api/projects/{project_id}/android/builds/{first['id']}/bundle"
        bundle = await client.get(url)
        assert bundle.status_code == 200 and bundle.content == host.bundles[job.id]
        assert done["size"] == len(bundle.content)
        second = await started(client, project_id)
        await until(lambda: finished(client, project_id, second["id"]))
        assert second["version_code"] == 2
        assert host.last("android").android == AndroidBuild(
            image=IMAGE, version_code=2, version_name="1.0.2"
        )
    kept = server.services.android.root / project_id
    assert len(list(kept.glob("*.aab"))) == 2
    async with server.services.db.session() as session, session.begin():
        await session.execute(delete(HostedApp))  # the hosted app was deleted first
    assert (await client.delete(f"/api/projects/{project_id}")).status_code == 204
    assert not kept.exists()  # the bundles go with the project


async def test_nothing_is_built_without_an_image_an_approval_a_host_or_while_one_runs(
    server: LiveServer, client: httpx.AsyncClient, host: HostStandIn
) -> None:
    url = "/api/projects/{}/android/builds"
    unhosted, _ = await app_project(server, client, server.services.dev_user_id)
    refused = await client.post(url.format(unhosted))
    assert refused.status_code == 409 and "staging first" in refused.text
    made = await client.post("/api/projects", json={"name": "Bare", "source": "app"})
    bare = made.json()["id"]
    assert "approved" in (await client.post(url.format(bare))).text
    host.holds["android"] = asyncio.Event()
    async with host_online(server.url, await host_token(server, host), host):
        project_id = await android_project(server, client)
        await started(client, project_id)
        await until(lambda: asyncio.sleep(0, "android" in host.kinds()))
        again = await client.post(url.format(project_id))
        assert again.status_code == 409 and "under way" in again.text
        server.services.settings.hosting.android_image = ""
        no_image = await client.post(url.format(project_id))
        assert no_image.status_code == 409 and "android_image" in no_image.text


async def test_no_android_app_or_a_failed_build_fails_with_the_reason(
    server: LiveServer, client: httpx.AsyncClient, host: HostStandIn
) -> None:
    async with host_online(server.url, await host_token(server, host), host):
        project_id, _ = await app_project(server, client, server.services.dev_user_id)
        staging = await deployed(client, project_id, "staging")
        await until(lambda: settled(client, project_id, staging))
        plain = await started(client, project_id)
        missing = await until(lambda: finished(client, project_id, plain["id"]))
        assert missing["status"] == "failed" and "android/gradlew" in missing["error"]
        assert "forge app new --android" in missing["hint"]
        assert "android" not in host.kinds()  # nothing went to the host
        await add_android(server, project_id)
        host.results["android"] = JobResult(
            ok=False, error="the Android build or its tests failed",
            log_tail="> Task :app:testReleaseUnitTest FAILED",
        )  # fmt: skip
        broken = await started(client, project_id)
        failed = await until(lambda: finished(client, project_id, broken["id"]))
        assert failed["status"] == "failed" and "FAILED" in failed["log_tail"]
        url = f"/api/projects/{project_id}/android/builds/{broken['id']}/bundle"
        assert (await client.get(url)).status_code == 409


async def test_a_restart_waits_for_the_same_build(tmp_path: Path) -> None:
    settings, port = server_settings(tmp_path / "data"), free_port()
    host = HostStandIn()
    with LiveServer(settings, port=port) as first:
        async with httpx.AsyncClient(base_url=first.url, headers=first.headers(),
                                     timeout=60) as client:  # fmt: skip
            online = host_online(first.url, await host_token(first, host), host)
            await online.__aenter__()
            project_id = await android_project(first, client)
            host.holds["android"] = asyncio.Event()
            build = await started(client, project_id)
            await until(lambda: asyncio.sleep(0, "android" in host.kinds()))
    try:
        with LiveServer(settings, port=port) as second:
            async with httpx.AsyncClient(base_url=second.url, headers=second.headers(),
                                         timeout=60) as client:  # fmt: skip
                host.holds["android"].set()  # the host finishes the build it had before
                done = await until(lambda: finished(client, project_id, build["id"]), 90)
    finally:
        await online.__aexit__(None, None, None)
    assert done["status"] == "built", done["error"]
    assert host.kinds().count("android") == 1
    shutil.rmtree(host.data_dir, ignore_errors=True)


def test_only_an_android_bundle_counts_as_one(tmp_path: Path) -> None:
    good, text, other = tmp_path / "a.aab", tmp_path / "b.aab", tmp_path / "c.aab"
    good.write_bytes(stand_in_bundle(3))
    text.write_text("the host's private key", encoding="utf-8")
    with zipfile.ZipFile(other, "w") as archive:
        archive.writestr("readme.txt", "no manifest")
    assert is_bundle(good) and not is_bundle(text) and not is_bundle(other)
    assert not is_bundle(tmp_path / "missing.aab")
