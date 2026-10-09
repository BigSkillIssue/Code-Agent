"""A new SwiftUI app for iPhone, iPad, Mac and Apple Watch (`forge apple new`).

The project is described by an XcodeGen `project.yml`; Xcode's project file is generated from
it and never edited. One code base (`Shared/`) runs on every platform, the Watch app ships
inside the iPhone app, and unit tests run on iPhone and Mac.
"""

import colorsys
import hashlib
import json
import plistlib
import re
import struct
import zlib
from pathlib import Path

NAME = re.compile(r"[A-Za-z][A-Za-z0-9]{0,29}")
BUNDLE_ID = re.compile(r"[A-Za-z0-9-]+(\.[A-Za-z0-9-]+)+")
ICON_SET = "Shared/Assets.xcassets/AppIcon.appiconset"
MAC_ICONS = [(16, 1), (16, 2), (32, 1), (32, 2), (128, 1), (128, 2), (256, 1), (256, 2), (512, 1),
             (512, 2)]  # fmt: skip
IPHONE_TURNS = " ".join(f"UIInterfaceOrientation{turn}" for turn in
                        ("Portrait", "LandscapeLeft", "LandscapeRight"))  # fmt: skip
IPAD_TURNS = f"{IPHONE_TURNS} UIInterfaceOrientationPortraitUpsideDown"

PROJECT_YML = """\
# The Xcode project is generated from this file (`xcodegen generate`; Forge does it before every
# build). Change this file, never {name}.xcodeproj.
name: {name}
options:
  minimumXcodeGenVersion: 2.41.0
  createIntermediateGroups: true
  deploymentTarget:
    iOS: "17.0"
    macOS: "14.0"
    watchOS: "10.0"
settings:
  base:
    SWIFT_VERSION: "6.0"
    MARKETING_VERSION: "1.0"
    CURRENT_PROJECT_VERSION: "1"
    GENERATE_INFOPLIST_FILE: "YES"
    INFOPLIST_KEY_CFBundleDisplayName: {name}
    INFOPLIST_KEY_ITSAppUsesNonExemptEncryption: "NO"
    ASSETCATALOG_COMPILER_APPICON_NAME: AppIcon
targets:
  {name}_iOS:
    type: application
    platform: iOS
    productName: {name}
    sources: [Shared]
    dependencies:
      - target: {name}_watchOS  # the Watch app ships inside the iPhone app
    settings:
      base:
        PRODUCT_BUNDLE_IDENTIFIER: {bundle}
        TARGETED_DEVICE_FAMILY: "1,2"
        INFOPLIST_KEY_UILaunchScreen_Generation: "YES"
        INFOPLIST_KEY_UIApplicationSceneManifest_Generation: "YES"
        INFOPLIST_KEY_UISupportedInterfaceOrientations_iPhone: "{iphone_turns}"
        INFOPLIST_KEY_UISupportedInterfaceOrientations_iPad: "{ipad_turns}"
    scheme:
      testTargets: [{name}_iOSTests]
  {name}_macOS:
    type: application
    platform: macOS
    productName: {name}
    sources: [Shared]
    entitlements:  # XcodeGen writes this file from these properties
      path: macOS/{name}.entitlements
      properties:
        com.apple.security.app-sandbox: true  # the Mac App Store requires the sandbox
    settings:
      base:
        PRODUCT_BUNDLE_IDENTIFIER: {bundle}
        ENABLE_HARDENED_RUNTIME: "YES"
        INFOPLIST_KEY_LSApplicationCategoryType: public.app-category.utilities
    scheme:
      testTargets: [{name}_macOSTests]
  {name}_watchOS:
    type: application
    platform: watchOS
    productName: {name}Watch
    sources: [Shared]
    settings:
      base:
        PRODUCT_BUNDLE_IDENTIFIER: {bundle}.watchkitapp
        INFOPLIST_KEY_WKCompanionAppBundleIdentifier: {bundle}
        INFOPLIST_KEY_WKRunsIndependentlyOfCompanionApp: "YES"
        SKIP_INSTALL: "YES"
    scheme: {{}}
  {name}_iOSTests:
    type: bundle.unit-test
    platform: iOS
    sources: [Tests]
    dependencies:
      - target: {name}_iOS
    settings:
      base:
        PRODUCT_BUNDLE_IDENTIFIER: {bundle}.tests
  {name}_macOSTests:
    type: bundle.unit-test
    platform: macOS
    sources: [Tests]
    dependencies:
      - target: {name}_macOS
    settings:
      base:
        PRODUCT_BUNDLE_IDENTIFIER: {bundle}.tests
"""

APP_SWIFT = """\
import SwiftUI

@main
struct {name}App: App {{
    var body: some Scene {{
        WindowGroup {{
            ContentView()
        }}
    }}
}}
"""

CONTENT_VIEW_SWIFT = """\
import SwiftUI

/// The app's only screen: a number that counts taps.
struct ContentView: View {
    @State private var counter = Counter()

    var body: some View {
        VStack(spacing: 12) {
            Text(counter.count, format: .number)
                .font(.system(.largeTitle, design: .rounded, weight: .bold))
                .monospacedDigit()
                .contentTransition(.numericText())
                .accessibilityLabel(Text("Count: \\(counter.count)"))
            Button("Count", systemImage: "plus") {
                withAnimation { counter.increment() }
            }
            .buttonStyle(.borderedProminent)
            Button("Reset", role: .destructive) {
                withAnimation { counter.reset() }
            }
            .disabled(counter.count == 0)
        }
        .padding()
        #if os(macOS)
        .frame(minWidth: 240, minHeight: 180)
        #endif
    }
}

#Preview {
    ContentView()
}
"""

COUNTER_SWIFT = """\
/// Counts up from zero. The app's model, free of UI code so tests can check it directly.
struct Counter: Equatable, Sendable {
    private(set) var count = 0

    mutating func increment() {
        count += 1
    }

    mutating func reset() {
        count = 0
    }
}
"""

TESTS_SWIFT = """\
import XCTest
@testable import {name}

final class CounterTests: XCTestCase {{
    func testCountsUpFromZero() {{
        var counter = Counter()
        counter.increment()
        counter.increment()
        XCTAssertEqual(counter.count, 2)
    }}

    func testResetGoesBackToZero() {{
        var counter = Counter()
        counter.increment()
        counter.reset()
        XCTAssertEqual(counter.count, 0)
    }}
}}
"""

GITIGNORE = """\
# generated from project.yml
*.xcodeproj/
DerivedData/
build/
xcuserdata/
.DS_Store
"""

README = """\
# {name}

A SwiftUI app for iPhone, iPad, Mac and Apple Watch, made with `forge apple new`.

- `project.yml` describes the Xcode project (XcodeGen). `xcodegen generate` writes
  `{name}.xcodeproj` from it; change the YAML, never the generated project.
- `Shared/` holds the code and assets of every platform; use `#if os(watchOS)` where they differ.
  The Watch app ships inside the iPhone app.
- `Tests/` holds the unit tests; they run on iPhone and Mac.
- `Shared/PrivacyInfo.xcprivacy` declares the data the app collects and the APIs it uses
  (nothing yet); keep it true when the app grows.
- The bundle id is `{bundle}`. Use your own (reverse domain) before uploading to Apple.
- The app icon in `{icons}` is a placeholder.

Build and test on a Mac with Xcode and XcodeGen (`brew install xcodegen`), for example
`xcodegen generate && xcodebuild -scheme {name}_iOS -destination "generic/platform=iOS Simulator"
build`, or let Forge do it: its `apple_build` and `apple_screenshot` tools work here.
"""


def name_problem(name: str) -> str:
    """Why `name` cannot name an app (it becomes a Swift module), or "" when it can."""
    if NAME.fullmatch(name):
        return ""
    return "use a letter followed by letters and digits, at most 30 (e.g. Tally or MyApp2)"


def bundle_problem(bundle_id: str) -> str:
    """Why `bundle_id` is no valid bundle identifier, or "" when it is."""
    if BUNDLE_ID.fullmatch(bundle_id):
        return ""
    return "use reverse domain parts of letters, digits and hyphens (e.g. com.example.tally)"


def app_files(name: str, bundle_id: str) -> dict[str, bytes]:
    """Every file of the new app by its path relative to the app's folder."""
    files = {
        "project.yml": PROJECT_YML.format(name=name, bundle=bundle_id, iphone_turns=IPHONE_TURNS,
                                          ipad_turns=IPAD_TURNS),
        f"Shared/{name}App.swift": APP_SWIFT.format(name=name),
        "Shared/ContentView.swift": CONTENT_VIEW_SWIFT,
        "Shared/Counter.swift": COUNTER_SWIFT,
        "Tests/CounterTests.swift": TESTS_SWIFT.format(name=name),
        ".gitignore": GITIGNORE,
        "README.md": README.format(name=name, bundle=bundle_id, icons=ICON_SET),
        "Shared/Assets.xcassets/Contents.json": catalog_json([]),
    }  # fmt: skip
    encoded = {path: text.encode() for path, text in files.items()}
    privacy = {"NSPrivacyTracking": False, "NSPrivacyTrackingDomains": [],
               "NSPrivacyCollectedDataTypes": [], "NSPrivacyAccessedAPITypes": []}  # fmt: skip
    encoded["Shared/PrivacyInfo.xcprivacy"] = plistlib.dumps(privacy)
    sandbox = {"com.apple.security.app-sandbox": True}
    encoded[f"macOS/{name}.entitlements"] = plistlib.dumps(sandbox)
    return encoded | icon_files(name)


def write_app(folder: Path, name: str, bundle_id: str) -> list[Path]:
    """Write the app into `folder`, which must be missing or empty; return the files written."""
    if folder.exists() and any(folder.iterdir()):
        raise FileExistsError(f"{folder} already exists and is not empty")
    written = []
    for relative, data in app_files(name, bundle_id).items():
        path = folder / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        written.append(path)
    return written


def icon_files(name: str) -> dict[str, bytes]:
    """A placeholder icon (a gradient in a colour of its own per name) in every size needed."""
    hue = hashlib.sha256(name.encode()).digest()[0] / 255
    top, bottom = rgb(hue, 0.45, 0.95), rgb(hue, 0.8, 0.55)
    images: list[dict[str, str]] = [
        {"filename": "icon-1024.png", "idiom": "universal", "platform": platform,
         "size": "1024x1024"}
        for platform in ("ios", "watchos")
    ]  # fmt: skip
    images += [{"filename": f"icon-{points * scale}.png", "idiom": "mac", "scale": f"{scale}x",
                "size": f"{points}x{points}"} for points, scale in MAC_ICONS]  # fmt: skip
    sizes = {int(i["filename"][5:-4]) for i in images}
    files = {f"{ICON_SET}/icon-{s}.png": gradient_png(s, top, bottom) for s in sorted(sizes)}
    files[f"{ICON_SET}/Contents.json"] = catalog_json(images).encode()
    return files


def catalog_json(images: list[dict[str, str]]) -> str:
    """An asset catalog's Contents.json (with `images` for an image set)."""
    body: dict[str, object] = {"images": images} if images else {}
    return json.dumps(body | {"info": {"author": "xcode", "version": 1}}, indent=2) + "\n"


def rgb(hue: float, saturation: float, value: float) -> tuple[int, int, int]:
    red, green, blue = colorsys.hsv_to_rgb(hue, saturation, value)
    return round(red * 255), round(green * 255), round(blue * 255)


def gradient_png(size: int, top: tuple[int, ...], bottom: tuple[int, ...]) -> bytes:
    """A square, opaque RGB PNG fading from `top` to `bottom` (App Store icons have no alpha)."""
    rows = bytearray()
    for y in range(size):
        share = y / max(size - 1, 1)
        pixel = bytes(round(a + (b - a) * share) for a, b in zip(top, bottom, strict=True))
        rows += b"\x00" + pixel * size  # filter type 0, then the row's pixels

    def chunk(kind: bytes, data: bytes) -> bytes:
        crc = zlib.crc32(kind + data)
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", crc)

    header = struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header)
            + chunk(b"IDAT", zlib.compress(bytes(rows), 9)) + chunk(b"IEND", b""))  # fmt: skip
