"""Signing an archive for the App Store and exporting it (W22): the .ipa (iPhone, iPad, Watch) or
the .pkg (Mac) that the server then uploads to App Store Connect.

It runs in a fresh VM made for this one job (or on the Mac in direct mode), never where the
project's own code ran. The certificate goes into a keychain of its own and the profiles into
Xcode's folders; both are removed again, whatever happens. No API key is ever here.
"""

import asyncio
import base64
import contextlib
import plistlib
import secrets
import shutil
import tarfile
from collections.abc import Awaitable, Callable
from pathlib import Path

from forge_macworker.wire import ExportParams, ExportResult, SigningMaterial

SIGNING = "signing.json"  # written by the worker, read once here, then deleted
Run = Callable[[list[str], Path], Awaitable[tuple[int, str]]]
PROFILE_DIRS = (
    Path.home() / "Library" / "Developer" / "Xcode" / "UserData" / "Provisioning Profiles",
    Path.home() / "Library" / "MobileDevice" / "Provisioning Profiles",
)
INSTALLER = "3rd Party Mac Developer Installer"  # how Xcode names the Mac installer certificate
EXPORT_TIMEOUT_S = 1800
LOG_TAIL = 20_000


async def run_command(argv: list[str], cwd: Path) -> tuple[int, str]:
    """Run a program; its exit code and its output."""
    proc = await asyncio.create_subprocess_exec(
        *argv, cwd=cwd, stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
    )  # fmt: skip
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), EXPORT_TIMEOUT_S)
    except BaseException:
        with contextlib.suppress(ProcessLookupError):
            proc.kill()
        await proc.wait()
        raise
    return proc.returncode or 0, out.decode("utf-8", "replace")


async def export_archive(
    params: ExportParams, folder: Path, archive: Path, target: Path, run: Run = run_command,
    profile_dirs: tuple[Path, ...] = PROFILE_DIRS,
) -> ExportResult:  # fmt: skip
    """Sign the archive in `archive` (a tar.gz) and pack the product into `target`."""
    signing = SigningMaterial.model_validate_json((folder / SIGNING).read_text("utf-8"))
    (folder / SIGNING).unlink()  # the key material stays on disk no longer than needed
    work = folder / "export"
    xcarchive = await asyncio.to_thread(unpack_xcarchive, archive, work / "in")
    keychain = Keychain(work / "forge-signing.keychain-db", secrets.token_hex(16), run)
    installed: list[Path] = []
    try:
        await keychain.open(signing)
        installed = install_profiles(signing, profile_dirs)
        options = work / "ExportOptions.plist"
        options.write_bytes(plistlib.dumps(export_options(params)))
        code, out = await run(["xcodebuild", "-exportArchive", "-archivePath", str(xcarchive),
                               "-exportPath", str(work / "out"), "-exportOptionsPlist",
                               str(options)], work)  # fmt: skip
        return await finished(params, code, out, work / "out", target)
    finally:
        await keychain.close()
        for path in installed:
            path.unlink(missing_ok=True)
        shutil.rmtree(work, ignore_errors=True)


def export_options(params: ExportParams) -> dict[str, object]:
    """ExportOptions.plist: App Store Connect, signed by hand with the given profiles."""
    options: dict[str, object] = {
        "method": "app-store-connect",
        "destination": "export",  # the server uploads, with the key that never leaves it
        "signingStyle": "manual",
        "teamID": params.team_id,
        "signingCertificate": "Apple Distribution",
        "provisioningProfiles": dict(params.profiles),
        "uploadSymbols": True,
        "manageAppVersionAndBuildNumber": False,
    }
    if params.platform == "macos":
        options["installerSigningCertificate"] = INSTALLER
    return options


async def finished(
    params: ExportParams, code: int, out: str, exported: Path, target: Path
) -> ExportResult:
    """The export's result; the product packed into `target` when there is one."""
    suffix = ".pkg" if params.platform == "macos" else ".ipa"
    products = sorted(exported.glob(f"*{suffix}")) if exported.is_dir() else []
    if code != 0 or not products:
        return ExportResult(ok=False, platform=params.platform, log_tail=out[-LOG_TAIL:])
    product = products[0]
    await asyncio.to_thread(pack_file, product, target)
    return ExportResult(ok=True, platform=params.platform, file_name=product.name,
                        size=product.stat().st_size, log_tail=out[-LOG_TAIL:])  # fmt: skip


class Keychain:
    """A keychain of its own for one export, in the search list only while it is needed."""

    def __init__(self, path: Path, password: str, run: Run) -> None:
        self.path, self.password, self.run = path, password, run
        self.previous: list[str] = []
        self.created = False

    async def open(self, signing: SigningMaterial) -> None:
        """Create and unlock it, import the certificates, and let Xcode's tools use them."""
        cwd = self.path.parent
        cwd.mkdir(parents=True, exist_ok=True)
        await self.checked(["security", "create-keychain", "-p", self.password, str(self.path)])
        self.created = True
        await self.checked(["security", "set-keychain-settings", str(self.path)])  # no auto-lock
        await self.checked(["security", "unlock-keychain", "-p", self.password, str(self.path)])
        for name, data in (("dist.p12", signing.p12_b64), ("inst.p12", signing.installer_p12_b64)):
            if data:
                p12 = cwd / name
                p12.write_bytes(base64.b64decode(data))
                p12.chmod(0o600)
                await self.checked(["security", "import", str(p12), "-k", str(self.path), "-P",
                                    signing.password, "-T", "/usr/bin/codesign", "-T",
                                    "/usr/bin/productbuild"])  # fmt: skip
                p12.unlink()
        await self.checked(["security", "set-key-partition-list", "-S", "apple-tool:,apple:",
                            "-s", "-k", self.password, str(self.path)])  # fmt: skip
        _, listed = await self.run(["security", "list-keychains", "-d", "user"], cwd)
        self.previous = [line.strip().strip('"') for line in listed.splitlines() if line.strip()]
        await self.checked(["security", "list-keychains", "-d", "user", "-s", str(self.path),
                            *self.previous])  # fmt: skip

    async def close(self) -> None:
        """Out of the search list and deleted (also after a failed open)."""
        cwd = self.path.parent
        if self.previous:
            await self.run(["security", "list-keychains", "-d", "user", "-s", *self.previous], cwd)
        if self.created:  # `security` knows the keychain even where the file is not visible
            await self.run(["security", "delete-keychain", str(self.path)], cwd)

    async def checked(self, argv: list[str]) -> None:
        code, out = await self.run(argv, self.path.parent)
        if code != 0:
            raise RuntimeError(f"{' '.join(argv[:2])} failed: {out.strip()[-500:]}")


def install_profiles(signing: SigningMaterial, dirs: tuple[Path, ...]) -> list[Path]:
    """The profiles where Xcode looks for them; the files written, to remove them later."""
    written = []
    for profile in signing.profiles:
        data = base64.b64decode(profile.data_b64)
        for folder in dirs:
            folder.mkdir(parents=True, exist_ok=True)
            path = folder / f"{profile.uuid}.mobileprovision"
            path.write_bytes(data)
            written.append(path)
    return written


def unpack_xcarchive(archive: Path, target: Path) -> Path:
    """The .xcarchive from its tar.gz (tar's "data" filter keeps it inside `target`)."""
    target.mkdir(parents=True, exist_ok=True)
    with tarfile.open(archive, "r:gz") as packed:
        packed.extractall(target, filter="data")
    found = sorted(target.glob("*.xcarchive"))
    if not found:
        raise RuntimeError("the job's archive holds no .xcarchive")
    return found[0]


def pack_file(path: Path, target: Path) -> None:
    """One file as a tar.gz for the server."""
    with tarfile.open(target, "w:gz") as packed:
        packed.add(path, arcname=path.name)
