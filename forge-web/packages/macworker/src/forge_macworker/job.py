"""Running one job where Xcode is: inside a project's macOS VM, or on the Mac in direct mode.

A job folder holds `job.json` (the JobOffer) and `source.tar.gz` (the packed project, or for an
export the archive to sign); the job leaves `result.json` (a JobResult) and, for archives and
exports, `archive.tar.gz`. `forge-mac-job <folder>` does this inside a VM; direct mode calls
`run_job` in the worker's own process.
"""

import asyncio
import shutil
import sys
import tarfile
import time
from collections.abc import Awaitable, Callable
from pathlib import Path

from forge.config import AppleConfig
from forge.local.xcode_builder import XcodeBuilder
from forge.ports import AppleBuilder, AppleBuildError, AppleBuildResult, ApplePlatform, AppleScreen

from forge_macworker.export import export_archive
from forge_macworker.store_shots import fit_screenshot
from forge_macworker.wire import ExportParams, ExportResult, JobOffer, JobResult, StoreSize

JOB, SOURCE, RESULT, ARCHIVE = "job.json", "source.tar.gz", "result.json", "archive.tar.gz"
BuilderFactory = Callable[[Path, Path], AppleBuilder]  # (project folder, its build data folder)
# (params, job folder, the archive to sign, where the product goes)
Exporter = Callable[[ExportParams, Path, Path, Path], Awaitable[ExportResult]]
Fitter = Callable[[AppleScreen, StoreSize], Awaitable[AppleScreen]]  # a picture to a store size


def xcode_builder(root: Path, data: Path) -> AppleBuilder:
    """The real builder: Forge's XcodeBuilder with this project's own build folder."""
    return XcodeBuilder(root, AppleConfig(), data_dir=data)


async def run_job(
    folder: Path, work: Path, make_builder: BuilderFactory = xcode_builder,
    exporter: Exporter = export_archive, fitter: Fitter = fit_screenshot,
) -> JobResult:  # fmt: skip
    """Run the job in `folder` with projects kept under `work`; write and return its result."""
    started = time.monotonic()
    offer = JobOffer.model_validate_json((folder / JOB).read_text("utf-8"))
    project = work / offer.project
    try:
        if offer.export is not None:
            exported = await exporter(offer.export, folder, folder / SOURCE, folder / ARCHIVE)
            result = JobResult(ok=True, export=exported)
        else:
            source = await asyncio.to_thread(unpack, folder / SOURCE, project / "src")
            builder = make_builder(source, project / "data")
            result = await perform(offer, builder, folder, fitter)
    except AppleBuildError as err:
        result = JobResult(ok=False, error=str(err), hint=err.hint)
    except (tarfile.TarError, OSError) as err:
        result = JobResult(ok=False, error=f"the job's files could not be unpacked: {err}")
    except RuntimeError as err:  # signing could not be set up
        result = JobResult(ok=False, error=str(err)[:2000])
    result.seconds = time.monotonic() - started
    (folder / RESULT).write_text(result.model_dump_json(), "utf-8")
    return result


async def perform(
    offer: JobOffer, builder: AppleBuilder, folder: Path, fitter: Fitter = fit_screenshot
) -> JobResult:
    """Build or photograph, as the offer says; an archive is packed into the job folder."""
    try:
        if offer.screenshot is not None:
            shot = offer.screenshot
            screen = await builder.screenshot(shot.platform, shot.device, shot.dark)
            if shot.fit is not None:  # for the App Store: exactly its size
                screen = await fitter(screen, shot.fit)
            return JobResult(ok=True, screen=screen)
        assert offer.build is not None
        params = offer.build
        if params.action == "archive" and params.build_number is not None:
            built = await release_archive(builder, params.platform, params.build_number,
                                          params.scheme)  # fmt: skip
        else:
            built = await builder.build(params.platform, params.action, params.scheme)
        if built.ok and built.artifact:
            await asyncio.to_thread(pack_archive, Path(built.artifact), folder / ARCHIVE)
        return JobResult(ok=True, build=built)
    finally:
        await builder.close()


async def release_archive(
    builder: AppleBuilder, platform: ApplePlatform, build_number: int, scheme: str | None
) -> AppleBuildResult:
    """An App Store archive (Forge's XcodeBuilder signs it ad hoc with the build number)."""
    make = getattr(builder, "release_archive", None)
    if make is None:
        raise AppleBuildError("this Mac's builder cannot make App Store archives",
                              hint="update forge on the Mac")  # fmt: skip
    built: AppleBuildResult = await make(platform, build_number, scheme)
    return built


def unpack(archive: Path, target: Path) -> Path:
    """A fresh copy of the project in `target`; tar's "data" filter refuses paths that leave
    it, absolute paths, devices and links that point outside."""
    if target.exists():
        shutil.rmtree(target)
    target.mkdir(parents=True)
    with tarfile.open(archive, "r:gz") as packed:
        packed.extractall(target, filter="data")
    return target


def pack_archive(xcarchive: Path, target: Path) -> None:
    """The .xcarchive folder as one tar.gz for the server."""
    with tarfile.open(target, "w:gz") as packed:
        packed.add(xcarchive, arcname=xcarchive.name)


def main(argv: list[str] | None = None) -> int:
    """`forge-mac-job <job folder> [work folder]` (inside a VM)."""
    args = sys.argv[1:] if argv is None else argv
    if not args:
        print("usage: forge-mac-job <job folder> [work folder]", file=sys.stderr)
        return 2
    work = Path(args[1]) if len(args) > 1 else Path.home() / "forge-work"
    result = asyncio.run(run_job(Path(args[0]), work))
    return 0 if result.ok else 1
