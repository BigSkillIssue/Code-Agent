"""What a packed .xcarchive holds, and the product of an export (W22b).

The server reads both without unpacking them to disk: the archive's Info.plist files say which
bundles need a profile and which version is built; the export's packed file is exactly one
.ipa or .pkg.
"""

import plistlib
import shutil
import tarfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

MAX_PLIST = 1024 * 1024
BUNDLES = (".app", ".appex")  # what a provisioning profile signs; frameworks need none
PRODUCTS = {"ios": ".ipa", "macos": ".pkg"}


class ArchiveProblem(Exception):
    """The archive or the export is not what it should be."""


@dataclass(frozen=True)
class ArchiveInfo:
    """The app in an archive: its identifier, version and build, and every bundle to sign."""

    bundle_id: str
    version: str
    build: str
    bundles: tuple[str, ...]  # the app's identifier first, then those inside it


def archive_info(packed: Path) -> ArchiveInfo:
    """Read the bundles and the version from a packed .xcarchive."""
    found: dict[PurePosixPath, dict[str, object]] = {}
    with tarfile.open(packed, "r:gz") as tar:
        for member in tar:
            path = PurePosixPath(member.name)
            if member.isfile() and path.name == "Info.plist" and member.size <= MAX_PLIST:
                extracted = tar.extractfile(member)
                if extracted is not None:
                    try:
                        found[path] = plistlib.loads(extracted.read())
                    except ValueError:  # not a property list (InvalidFileException is one)
                        continue
    bundles = sorted((p for p in found if signed_bundle(p)), key=lambda p: len(p.parts))
    identifiers = [str(found[p].get("CFBundleIdentifier", "")) for p in bundles]
    if not identifiers or not all(identifiers):
        raise ArchiveProblem("the archive holds no app with a bundle identifier")
    main = found[bundles[0]]
    return ArchiveInfo(
        bundle_id=identifiers[0], version=str(main.get("CFBundleShortVersionString", "")),
        build=str(main.get("CFBundleVersion", "")), bundles=tuple(dict.fromkeys(identifiers)),
    )  # fmt: skip


def signed_bundle(plist: PurePosixPath) -> bool:
    """Whether an Info.plist belongs to an app or extension among the archive's products (on
    the Mac it sits in the bundle's Contents folder)."""
    parts = plist.parts
    if "Products" not in parts or "dSYMs" in parts:
        return False
    folder = plist.parent.parent if plist.parent.name == "Contents" else plist.parent
    return folder.suffix in BUNDLES


def product_of(packed: Path, platform: str, folder: Path) -> Path:
    """The .ipa or .pkg an export sent (one file, nothing else), copied into `folder`."""
    suffix = PRODUCTS[platform]
    with tarfile.open(packed, "r:gz") as tar:
        members = tar.getmembers()
        if len(members) != 1 or not members[0].isfile():
            raise ArchiveProblem("the export sent more or less than one file")
        name = PurePosixPath(members[0].name)
        if len(name.parts) != 1 or name.suffix != suffix or name.name.startswith("."):
            raise ArchiveProblem(f"the export sent {name}, not a {suffix} file")
        source = tar.extractfile(members[0])
        if source is None:
            raise ArchiveProblem("the export's file cannot be read")
        folder.mkdir(parents=True, exist_ok=True)
        target = folder / name.name
        with target.open("wb") as out:
            shutil.copyfileobj(source, out)
    return target
