"""One msix job: the packed commit unpacked into the job's own folder, the app's tests run, the
app packaged for the Microsoft Store (unsigned: the Store signs it), and only the package kept.

Commands run through a `Run` (the runner), so tests can stand in for dotnet.
"""

import os
import re
import tarfile
from collections.abc import Awaitable, Callable
from pathlib import Path
from xml.etree import ElementTree

from forge_winworker.wire import JobResult, MsixParams

Run = Callable[[list[str], Path], Awaitable[tuple[int, str]]]  # argv, cwd -> exit code, output
SOURCE = "source.tar.gz"
PACKAGE = "package.msixupload"  # what the client uploads
APP = Path("windows/App/App.csproj")  # `forge app new --windows` made the app
TESTS = Path("windows/Tests/Tests.csproj")
MANIFEST = Path("windows/App/Package.appxmanifest")
IDENTITY_VERSION = re.compile(r'(<Identity\b[^>]*\bVersion=")[^"]*(")')


class SourceError(Exception):
    """The packed commit cannot be used."""


def unpack(archive: Path, target: Path) -> None:
    """The commit's files, refusing links and paths outside `target` (tar's data filter)."""
    target.mkdir(parents=True, exist_ok=True)
    try:
        with tarfile.open(archive, "r:gz") as tar:
            tar.extractall(target, filter="data")
    except (tarfile.TarError, OSError) as err:
        raise SourceError(f"the packed commit cannot be unpacked: {err}") from None


def set_version(manifest: Path, version: str) -> None:
    """The package's version in its manifest (every upload to the Store needs a higher one)."""
    text = manifest.read_text(encoding="utf-8")
    changed, count = IDENTITY_VERSION.subn(rf"\g<1>{version}\g<2>", text, count=1)
    if count != 1:
        raise SourceError("Package.appxmanifest has no <Identity Version=...>")
    manifest.write_text(changed, encoding="utf-8")


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


async def msix_job(folder: Path, params: MsixParams, run: Run) -> JobResult:
    """Test and package the app in `folder`; the package ends up as `folder/PACKAGE`."""
    source = folder / "src"
    try:
        unpack(folder / SOURCE, source)
        if not (source / APP).is_file():
            return JobResult(ok=False, error="the product has no Windows app (windows/App)",
                             hint="make one with `forge app new --windows`")  # fmt: skip
        set_version(source / MANIFEST, params.version)
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
    return await packaged(folder, source, params, run, passed)


async def packaged(
    folder: Path, source: Path, params: MsixParams, run: Run, passed: int
) -> JobResult:
    """The app packaged for the Store upload; the package moved to `folder/PACKAGE`."""
    out = folder / "out"
    code, output = await run(["dotnet", "publish", str(APP), "-c", "Release",
                              f"-p:Platform={params.platform}",
                              f"-p:RuntimeIdentifier=win-{params.platform}",
                              "-p:GenerateAppxPackageOnBuild=true",
                              "-p:UapAppxPackageBuildMode=StoreUpload",
                              "-p:AppxPackageSigningEnabled=false",
                              f"-p:AppxPackageDir={out}{os.sep}"], source)  # fmt: skip
    made = sorted(out.rglob("*.msixupload"))
    if code != 0 or not made:
        error = "packaging the app failed" if code != 0 else "the build made no .msixupload"
        return JobResult(ok=False, error=error, tests_passed=passed, log_tail=tail(output))
    made[0].replace(folder / PACKAGE)
    size = (folder / PACKAGE).stat().st_size
    return JobResult(ok=True, tests_passed=passed, file_name=made[0].name, size=size,
                     log_tail=tail(output))  # fmt: skip
