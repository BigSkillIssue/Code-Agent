"""The two kinds of jobs, each in a folder of its own.

A build job unpacks the packed commit, runs the app's tests and publishes the app; it runs the
product's code. When no signing machine takes over it also packs the app with Velopack
(unsigned); else it sends the published app. A pack job, on a signing machine, unpacks a
published app it got from the server and packs it with Velopack, which signs every file with
the machine's own signing command. Only what is sent back is kept.

Commands run through a `Run` (the runner), so tests can stand in for dotnet and vpk.
"""

import logging
import os
import re
import shutil
import tarfile
import zipfile
from collections.abc import Awaitable, Callable
from pathlib import Path, PurePosixPath
from xml.etree import ElementTree

from forge_winworker.wire import PACK_ID, BuildParams, JobResult, PackParams, release_files

Run = Callable[[list[str], Path], Awaitable[tuple[int, str]]]  # argv, cwd -> exit code, output
SOURCE = "source.tar.gz"  # what a build job gets
APP_ZIP = "app.zip"  # what a pack job gets: the published app (`app/...`) and its icon
UPLOAD = "upload.zip"  # what the client sends: the published app, or the release
APP = Path("windows/App/App.csproj")  # `forge app new --windows` made the app
TESTS = Path("windows/Tests/Tests.csproj")
ICON = Path("windows/App/App.ico")
ASSEMBLY = re.compile(r"<AssemblyName>\s*([^<\s]+)\s*</AssemblyName>")
MAX_UNPACKED = 4 * 1024**3  # bytes of a published app, unpacked
MAX_ICON = 1024 * 1024
RESERVED = {"CON", "PRN", "AUX", "NUL", *(f"COM{n}" for n in range(10)),
            *(f"LPT{n}" for n in range(10))}  # fmt: skip
VELOPACK_HINT = ("is vpk installed on this machine (`dotnet tool install -g vpk`)? Does the "
                 "app call VelopackApp.Build().Run() first (`forge app new` does)?")  # fmt: skip
log = logging.getLogger(__name__)


class SourceError(Exception):
    """The packed commit or the published app cannot be used."""


def unpack(archive: Path, target: Path) -> None:
    """The commit's files, refusing links and paths outside `target` (tar's data filter)."""
    target.mkdir(parents=True, exist_ok=True)
    try:
        with tarfile.open(archive, "r:gz") as tar:
            tar.extractall(target, filter="data")
    except (tarfile.TarError, OSError) as err:
        raise SourceError(f"the packed commit cannot be unpacked: {err}") from None


def assembly_name(project: Path) -> str:
    """The app's assembly name (its .exe and the name Velopack installs it under)."""
    found = ASSEMBLY.search(project.read_text(encoding="utf-8", errors="replace"))
    name = found.group(1) if found else ""
    if not re.fullmatch(PACK_ID, name):
        raise SourceError("windows/App/App.csproj needs an <AssemblyName> of letters and digits")
    return name


def counted(results: Path) -> tuple[int, int]:
    """Passed and failed tests from dotnet test's TRX files."""
    passed = failed = 0
    for trx in results.glob("*.trx"):
        for element in ElementTree.parse(trx).iter():
            if element.tag.endswith("Counters"):
                passed += int(element.get("passed", 0))
                failed += int(element.get("failed", 0))
    return passed, failed


def tail(output: str) -> str:
    return output[-20_000:]


def zip_app(publish: Path, icon: Path | None, target: Path) -> int:
    """The published app as `app/...` in a ZIP, with its icon; the ZIP's bytes."""
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as archive:
        for root, _, files in os.walk(publish):  # links are not followed: nothing from outside
            for name in sorted(files):
                path = Path(root) / name
                if path.is_file() and not path.is_symlink():
                    archive.write(path, "app/" + path.relative_to(publish).as_posix())
        if icon is not None:
            archive.write(icon, "icon.ico")
    return target.stat().st_size


def safe_parts(name: str) -> tuple[str, ...]:
    """The parts of a ZIP entry's path inside `app/`, refusing anything that leaves it."""
    parts = PurePosixPath(name).parts
    bad = (not name.startswith("app/") or "\\" in name or ":" in name or len(parts) < 2
           or any(p in ("", ".", "..") or p.split(".")[0].upper() in RESERVED
                  or p.endswith((" ", ".")) for p in parts))  # fmt: skip
    if bad:
        raise SourceError(f"the published app has a file outside its folder: {name[:200]}")
    return parts[1:]


def unzip_app(archive: Path, publish: Path, icon: Path) -> Path | None:
    """The published app from its ZIP into `publish`; its icon, if it came with one."""
    total, has_icon = 0, False
    try:
        with zipfile.ZipFile(archive) as found:
            for entry in found.infolist():
                if entry.is_dir():
                    continue
                total += entry.file_size
                if total > MAX_UNPACKED:
                    raise SourceError("the published app is larger than a pack job takes")
                if entry.filename == "icon.ico" and entry.file_size <= MAX_ICON:
                    target, has_icon = icon, True
                else:
                    target = publish.joinpath(*safe_parts(entry.filename))
                target.parent.mkdir(parents=True, exist_ok=True)
                with found.open(entry) as source, target.open("wb") as out:
                    shutil.copyfileobj(source, out)
    except (zipfile.BadZipFile, OSError) as err:
        raise SourceError(f"the published app cannot be unpacked: {err}") from None
    return icon if has_icon else None


def zip_release(release: Path, names: list[str], target: Path) -> int:
    """The release's files in a ZIP (stored: they are packed already); the ZIP's bytes."""
    with zipfile.ZipFile(target, "w", zipfile.ZIP_STORED) as archive:
        for name in names:
            archive.write(release / name, name)
    return target.stat().st_size


async def build_job(folder: Path, params: BuildParams, run: Run) -> JobResult:
    """Test and publish the app in `folder` (and pack it); what to send ends up in UPLOAD."""
    source = folder / "src"
    try:
        unpack(folder / SOURCE, source)
        if not (source / APP).is_file():
            return JobResult(ok=False, error="the product has no Windows app (windows/App)",
                             hint="make one with `forge app new --windows`")  # fmt: skip
        pack_id = assembly_name(source / APP)
    except SourceError as err:
        return JobResult(ok=False, error=str(err))
    results = folder / "results"
    code, output = await run(["dotnet", "test", str(TESTS), "-c", "Release",
                              "--logger", "trx", "--results-directory", str(results)],
                             source)  # fmt: skip
    passed, failed = counted(results)
    if code != 0:
        return JobResult(ok=False, error="the Windows app's tests failed", tests_passed=passed,
                         tests_failed=failed, log_tail=tail(output))  # fmt: skip
    publish = folder / "publish"
    code, output = await run(["dotnet", "publish", str(APP), "-c", "Release",
                              f"-p:Platform={params.platform}",
                              f"-p:RuntimeIdentifier=win-{params.platform}",
                              f"-p:Version={params.version}", "-o", str(publish)],
                             source)  # fmt: skip
    if code != 0 or not (publish / f"{pack_id}.exe").is_file():
        error = "publishing the app failed" if code != 0 else f"the build made no {pack_id}.exe"
        return JobResult(ok=False, error=error, tests_passed=passed, log_tail=tail(output))
    icon = source / ICON if (source / ICON).is_file() else None
    if not params.pack:  # a signing machine packs it
        size = zip_app(publish, icon, folder / UPLOAD)
        return JobResult(ok=True, tests_passed=passed, pack_id=pack_id, size=size,
                         log_tail=tail(output))  # fmt: skip
    pack = PackParams(version=params.version, platform=params.platform, title=params.title,
                      pack_id=pack_id)  # fmt: skip
    packed = await velopack(folder, publish, icon, pack, run, "")
    return packed.model_copy(update={"tests_passed": passed})


async def pack_job(folder: Path, params: PackParams, run: Run, sign_template: str) -> JobResult:
    """Pack and sign the published app the server sent; the release ends up in UPLOAD."""
    publish = folder / "publish"
    try:
        icon = unzip_app(folder / APP_ZIP, publish, folder / "icon.ico")
    except SourceError as err:
        return JobResult(ok=False, error=str(err))
    if not (publish / f"{params.pack_id}.exe").is_file():
        return JobResult(ok=False, error=f"the published app has no {params.pack_id}.exe")
    return await velopack(folder, publish, icon, params, run, sign_template)


async def velopack(
    folder: Path, publish: Path, icon: Path | None, params: PackParams, run: Run, sign: str
) -> JobResult:
    """The installer, the ZIP and the update files Velopack makes of `publish`, signed with
    the `sign` command when there is one."""
    release = folder / "release"
    command = ["vpk", "pack", "--packId", params.pack_id, "--packVersion", params.version,
               "--packDir", str(publish), "--mainExe", f"{params.pack_id}.exe",
               "--packTitle", params.title, "--runtime", f"win-{params.platform}",
               "--delta", "None", "--outputDir", str(release)]  # fmt: skip
    if icon is not None:
        command += ["--icon", str(icon)]
    if sign:  # a signing machine never starts the product's program, not even to check it
        command += ["--signTemplate", sign, "--skipVeloAppCheck"]
    code, output = await run(command, folder)
    if sign:  # the signing command and its output stay on this machine
        log.info("vpk on %s:\n%s", params.pack_id, tail(output))
        output = "(the signing machine keeps its log)"
    names = release_files(params.pack_id, params.version)
    missing = [name for name, needed in names.items() if needed and not (release / name).is_file()]
    if code != 0 or missing:
        return JobResult(ok=False, error="packing the app with Velopack failed",
                         hint=VELOPACK_HINT, log_tail=tail(output))  # fmt: skip
    kept = [name for name in names if (release / name).is_file()]
    size = zip_release(release, kept, folder / UPLOAD)
    return JobResult(ok=True, pack_id=params.pack_id, signed=bool(sign), size=size,
                     log_tail=tail(output))  # fmt: skip
