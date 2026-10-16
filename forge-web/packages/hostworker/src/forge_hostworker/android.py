"""Android builds (W37a): an app's Android app built from its packed source with Gradle, in a
throwaway gVisor container like every build: no secrets (the bundle is unsigned; Forge Web signs
it for Google Play), only the build network (Maven, Google, Gradle), a read-only image and limits.
The unit tests run first; the release bundle is left in `outgoing` for the client to upload to
Forge Web, and nothing of the build stays on the host.
"""

import asyncio
import shutil
from pathlib import Path
from typing import TYPE_CHECKING

from forge_hostworker.build import SourceError, hand_over, unpack
from forge_hostworker.docker import hardened
from forge_hostworker.firewall import BUILD_NETWORK
from forge_hostworker.wire import CheckState, HostJob, JobResult

if TYPE_CHECKING:  # the runner calls this module, which only borrows its parts
    from forge_hostworker.runner import HostRunner

ANDROID_TIMEOUT_S = 3600
MEMORY = "6g"  # Gradle and the Kotlin compiler
CPUS = "4"
PIDS = "4096"
FOLDER = "android"  # where a product keeps its Android app (`forge app new --android`)
BUNDLE = Path("app/build/outputs/bundle/release/app-release.aab")


def bundle_path(data_dir: Path, job_id: str) -> Path:
    """Where an android job leaves its bundle for the client to upload."""
    return data_dir / "outgoing" / f"{job_id}.aab"


def android_args(job: HostJob, folder: Path, user: str, network: str) -> list[str]:
    """`docker run` for the Gradle build: tests, then the release bundle."""
    if job.android is None:
        raise ValueError("an android job says what to build with")
    build = job.android
    return [
        "run", "--rm", *hardened(user), "--read-only",
        "--tmpfs", "/tmp:rw,exec,size=4096m",
        "--network", network,
        "--memory", MEMORY, "--cpus", CPUS, "--pids-limit", PIDS,
        "-v", f"{folder}:/app", "-w", "/app",
        "-e", "HOME=/tmp", "-e", "GRADLE_USER_HOME=/tmp/gradle",
        "--label", f"forge.app={job.app}", "--label", "forge.role=android",
        build.image,
        "./gradlew", "--no-daemon", f"-PversionCode={build.version_code}",
        f"-PversionName={build.version_name}", "testReleaseUnitTest", "bundleRelease",
    ]  # fmt: skip


async def android_job(runner: "HostRunner", job: HostJob, source: Path) -> JobResult:
    """Build the bundle and leave it in `outgoing`; a failed build says why."""
    work = runner.data_dir / "android" / job.id
    try:
        await asyncio.to_thread(unpack, source, work)
        folder = work / FOLDER
        if not (folder / "gradlew").is_file():
            return JobResult(ok=False, error="the product has no Android app (android/gradlew)",
                             hint="make one with `forge app new --android`")  # fmt: skip
        if runner.owner is not None:
            await asyncio.to_thread(hand_over, work, runner.owner)
        await runner.ensure_build_network()
        built = await runner.docker(*android_args(job, folder, runner.user, BUILD_NETWORK),
                                    timeout=ANDROID_TIMEOUT_S)  # fmt: skip
        if built.code != 0:
            return JobResult(ok=False, error="the Android build or its tests failed",
                             log_tail=built.tail())  # fmt: skip
        bundle = folder / BUNDLE
        if not bundle.is_file():
            return JobResult(ok=False, error="the build made no release bundle",
                             log_tail=built.tail())  # fmt: skip
        target = bundle_path(runner.data_dir, job.id)
        target.parent.mkdir(parents=True, exist_ok=True)
        await asyncio.to_thread(shutil.move, bundle, target)
    except SourceError as error:
        return JobResult(ok=False, error=f"the source was refused: {error}")
    finally:
        await asyncio.to_thread(shutil.rmtree, work, True)
    made = CheckState(name="android", ok=True, detail=f"{target.stat().st_size} bytes")
    return JobResult(ok=True, checks=[made])
