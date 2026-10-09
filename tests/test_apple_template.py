"""`forge apple new`: a SwiftUI app for iPhone, iPad, Mac and Apple Watch (built for real in CI)."""

import json
import plistlib
import struct
import zlib
from pathlib import Path

import pytest

from forge.apple_template import app_files, bundle_problem, name_problem, write_app
from forge.cli import main
from forge.local.xcode_builder import pick_scheme
from forge.runtime.apple import is_apple_project

ICONS = "Shared/Assets.xcassets/AppIcon.appiconset"


def test_names_and_bundle_ids_are_checked() -> None:
    assert name_problem("Tally") == "" and name_problem("Tally2") == ""
    for bad in ("", "my app", "2Tally", "Tally_App", "Tällý", "T" * 40):
        assert name_problem(bad), bad
    assert bundle_problem("com.example.tally") == "" and bundle_problem("de.max-d.app") == ""
    for bad in ("tally", "com..tally", "com.example.", "com.example.tally app", "com.ex_ample.t"):
        assert bundle_problem(bad), bad


def test_the_app_has_a_target_for_every_device() -> None:
    files = app_files("Tally", "com.acme.tally")
    spec = files["project.yml"].decode()
    for target in ("Tally_iOS", "Tally_macOS", "Tally_watchOS", "Tally_iOSTests",
                   "Tally_macOSTests"):  # fmt: skip
        assert f"\n  {target}:\n" in spec, target
    assert 'TARGETED_DEVICE_FAMILY: "1,2"' in spec  # iPhone and iPad
    assert "PRODUCT_BUNDLE_IDENTIFIER: com.acme.tally.watchkitapp" in spec
    assert "INFOPLIST_KEY_WKCompanionAppBundleIdentifier: com.acme.tally" in spec
    assert "- target: Tally_watchOS" in spec  # the Watch app ships inside the iPhone app
    assert "\t" not in spec and all((len(line) - len(line.lstrip())) % 2 == 0
                                    for line in spec.splitlines())  # fmt: skip
    assert b"@main" in files["Shared/TallyApp.swift"]
    assert b"struct ContentView: View" in files["Shared/ContentView.swift"]
    assert b"@testable import Tally" in files["Tests/CounterTests.swift"]
    assert b"*.xcodeproj" in files[".gitignore"]  # generated from project.yml


def png_size(data: bytes) -> tuple[int, int]:
    """Width and height of a PNG, after checking it is a whole, opaque RGB image."""
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    chunks, at = {}, 8
    while at < len(data):
        (length,) = struct.unpack(">I", data[at : at + 4])
        kind, body = data[at + 4 : at + 8], data[at + 8 : at + 8 + length]
        (crc,) = struct.unpack(">I", data[at + 8 + length : at + 12 + length])
        assert crc == zlib.crc32(kind + body)
        chunks[kind] = body
        at += 12 + length
    width, height, depth, color = struct.unpack(">IIBB", chunks[b"IHDR"][:10])
    assert (depth, color) == (8, 2)  # 8-bit RGB: App Store icons may not have alpha
    assert len(zlib.decompress(chunks[b"IDAT"])) == height * (1 + 3 * width)
    return width, height


def test_icons_are_opaque_pngs_of_the_sizes_the_catalog_names() -> None:
    files = app_files("Tally", "com.example.tally")
    images = json.loads(files[f"{ICONS}/Contents.json"])["images"]
    assert {i.get("platform", i["idiom"]) for i in images} == {"ios", "watchos", "mac"}
    for image in images:
        points = float(image["size"].split("x")[0])
        pixels = int(points * int(image.get("scale", "1x").rstrip("x")))
        assert png_size(files[f"{ICONS}/{image['filename']}"]) == (pixels, pixels)


def test_privacy_manifest_and_mac_sandbox() -> None:
    files = app_files("Tally", "com.example.tally")
    privacy = plistlib.loads(files["Shared/PrivacyInfo.xcprivacy"])
    assert privacy["NSPrivacyTracking"] is False
    assert privacy["NSPrivacyCollectedDataTypes"] == privacy["NSPrivacyAccessedAPITypes"] == []
    sandbox = plistlib.loads(files["macOS/Tally.entitlements"])
    assert sandbox == {"com.apple.security.app-sandbox": True}  # the Mac App Store needs it
    spec = files["project.yml"].decode()  # XcodeGen writes the file from here: both must agree
    assert "path: macOS/Tally.entitlements" in spec
    assert "com.apple.security.app-sandbox: true" in spec


@pytest.mark.parametrize("name", ["Tally", "Stopwatch", "Radios", "Macro"])
def test_the_builder_finds_each_platforms_scheme(name: str) -> None:
    schemes = [f"{name}_iOS", f"{name}_macOS", f"{name}_watchOS"]
    assert pick_scheme(schemes, "ios") == pick_scheme(schemes, "ipados") == f"{name}_iOS"
    assert pick_scheme(schemes, "macos") == f"{name}_macOS"
    assert pick_scheme(schemes, "watchos") == f"{name}_watchOS"
    assert pick_scheme(["Only"], "watchos") == "Only"
    assert pick_scheme(["Alpha", "Beta"], "ios") is None


def test_write_app_refuses_a_folder_that_is_in_use(tmp_path: Path) -> None:
    written = write_app(tmp_path / "Tally", "Tally", "com.example.tally")
    assert tmp_path / "Tally" / "project.yml" in written and is_apple_project(tmp_path / "Tally")
    with pytest.raises(FileExistsError):
        write_app(tmp_path / "Tally", "Tally", "com.example.tally")


def test_forge_apple_new(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["-C", str(tmp_path), "apple", "new", "Tally", "--bundle-id", "de.max.t"]) == 0
    spec = (tmp_path / "Tally" / "project.yml").read_text()
    assert "PRODUCT_BUNDLE_IDENTIFIER: de.max.t\n" in spec
    out = capsys.readouterr().out
    assert "Tally" in out and "xcodegen" in out
    assert main(["-C", str(tmp_path), "apple", "new", "Tally"]) == 1  # already there
    assert "already" in capsys.readouterr().err
    assert main(["-C", str(tmp_path), "apple", "new", "my app"]) == 1
    assert "letter" in capsys.readouterr().err
    assert main(["-C", str(tmp_path), "apple", "new", "Other"]) == 0
    assert "com.example.other" in (tmp_path / "Other" / "project.yml").read_text()
