"""What the Windows machines send, checked before the server keeps it (W39).

A build machine runs the product's code, so nothing it sends is trusted: a published app for a
signing machine holds only files under `app/` (and its icon), with the app's .exe; a Velopack
release holds only its own files, its update list names exactly this version's full package
with that package's SHA-256, its installer is a Windows program, and a release that should be
signed has an Authenticode signature on its installer.
"""

import hashlib
import json
import shutil
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any

from forge_winworker.wire import release_files

MAX_APP_BYTES = 4 * 1024**3  # a published app, unpacked (the worker's own limit)
SECURITY_DIRECTORY = 4  # IMAGE_DIRECTORY_ENTRY_SECURITY: where a PE file keeps its signature


class ReleaseProblem(Exception):
    """What a Windows machine sent cannot be used."""


def check_app(archive: Path, pack_id: str) -> None:
    """A published app as a build machine sends it for a signing machine."""
    try:
        with zipfile.ZipFile(archive) as found:
            entries = [e for e in found.infolist() if not e.is_dir()]
    except (zipfile.BadZipFile, OSError) as err:
        raise ReleaseProblem(f"the published app is not a ZIP: {err}") from None
    total = 0
    for entry in entries:
        parts = PurePosixPath(entry.filename).parts
        inside = entry.filename.startswith("app/") and ".." not in parts and len(parts) > 1
        if not (inside or entry.filename == "icon.ico") or "\\" in entry.filename:
            raise ReleaseProblem(f"the published app has a file outside it: {entry.filename[:200]}")
        total += entry.file_size
    if total > MAX_APP_BYTES:
        raise ReleaseProblem("the published app is larger than a signing machine takes")
    if f"app/{pack_id}.exe" not in {e.filename for e in entries}:
        raise ReleaseProblem(f"the published app has no {pack_id}.exe")


def sha256_of(path: Path) -> str:
    """A file's SHA-256, upper case like Velopack's."""
    digest = hashlib.sha256()
    with path.open("rb") as file:
        while part := file.read(1024 * 1024):
            digest.update(part)
    return digest.hexdigest().upper()


def signature_size(path: Path) -> int:
    """The bytes of a Windows program's Authenticode signature (0: none, or no program)."""
    with path.open("rb") as file:
        head = file.read(4096)
    if head[:2] != b"MZ" or len(head) < 0x40:
        return 0
    pe = int.from_bytes(head[0x3C:0x40], "little")
    optional = pe + 24
    if head[pe : pe + 4] != b"PE\0\0" or len(head) < optional + 2:
        return 0
    magic = int.from_bytes(head[optional : optional + 2], "little")
    counts = {0x10B: 92, 0x20B: 108}  # PE32 and PE32+: where NumberOfRvaAndSizes is
    if magic not in counts:
        return 0
    where = optional + counts[magic]
    directories = int.from_bytes(head[where : where + 4], "little")
    entry = where + 4 + 8 * SECURITY_DIRECTORY
    if directories <= SECURITY_DIRECTORY or len(head) < entry + 8:
        return 0
    return int.from_bytes(head[entry + 4 : entry + 8], "little")


def unpack_release(
    upload: Path, target: Path, pack_id: str, version: str, signed: bool, limit: int
) -> dict[str, Any]:
    """The release's files from its ZIP into `target`, checked; name -> size and SHA-256."""
    names = release_files(pack_id, version)
    try:
        with zipfile.ZipFile(upload) as found:
            entries = found.infolist()
            sent = [entry.filename for entry in entries]
            strange = [name for name in sent if name not in names]
            if strange or len(set(sent)) != len(sent):
                raise ReleaseProblem(f"the release has files it should not have: {strange[:5]}")
            missing = [name for name, needed in names.items() if needed and name not in sent]
            if missing:
                raise ReleaseProblem(f"the release lacks {', '.join(missing)}")
            if sum(entry.file_size for entry in entries) > limit:
                raise ReleaseProblem("the release is larger than this server keeps")
            shutil.rmtree(target, ignore_errors=True)
            target.mkdir(parents=True)
            for entry in entries:
                with found.open(entry) as source, (target / entry.filename).open("wb") as out:
                    shutil.copyfileobj(source, out)
    except (zipfile.BadZipFile, OSError) as err:
        raise ReleaseProblem(f"the release is not a ZIP: {err}") from None
    files = {name: {"size": (target / name).stat().st_size, "sha256": sha256_of(target / name)}
             for name in sent}  # fmt: skip
    check_release(target, files, pack_id, version, signed)
    return files


def check_release(
    folder: Path, files: dict[str, Any], pack_id: str, version: str, signed: bool
) -> None:
    """The update list names this version's package rightly; the installer is a program,
    signed when it should be."""
    package = f"{pack_id}-{version}-full.nupkg"
    try:
        listed = json.loads((folder / "releases.win.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        listed = None
    assets = listed.get("Assets") if isinstance(listed, dict) else None
    if not isinstance(assets, list) or len(assets) != 1 or not isinstance(assets[0], dict):
        raise ReleaseProblem("releases.win.json does not list exactly one package")
    asset = assets[0]
    wanted = {"PackageId": pack_id, "Version": version, "Type": "Full", "FileName": package,
              "SHA256": files[package]["sha256"], "Size": files[package]["size"]}  # fmt: skip
    wrong = [key for key, value in wanted.items() if asset.get(key) != value]
    if wrong:
        raise ReleaseProblem(f"releases.win.json does not match the package: {', '.join(wrong)}")
    setup = folder / f"{pack_id}-win-Setup.exe"
    with setup.open("rb") as file:
        if file.read(2) != b"MZ":
            raise ReleaseProblem("the installer is not a Windows program")
    if signed and signature_size(setup) == 0:
        raise ReleaseProblem("the installer is not signed, though a signing machine packed it")
