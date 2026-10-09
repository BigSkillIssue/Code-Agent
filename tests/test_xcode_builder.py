"""The local AppleBuilder (XcodeGen, xcodebuild, simctl) against fake Apple tools that log calls."""

import base64
import json
import os
import sys
from pathlib import Path
from typing import Any

import pytest

from forge.config import AppleConfig
from forge.local.xcode_builder import XcodeBuilder
from forge.ports import AppleBuildError

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="the fake tools are scripts")

PNG = b"\x89PNG\r\n\x1a\nfake"
TOOLS = ("xcodebuild", "xcrun", "xcodegen", "plutil", "screencapture")
FAKE = """#!{python}
import json, os, shutil, sys, time
from pathlib import Path

name, args = Path(sys.argv[0]).name, sys.argv[1:]
with open(os.environ["FAKE_LOG"], "a") as log:
    log.write(json.dumps([name, *args]) + "\\n")
state = json.loads(Path(os.environ["FAKE_STATE"]).read_text())


def after(flag):
    return args[args.index(flag) + 1]


if name == "xcodegen":
    Path("Demo.xcodeproj").mkdir(exist_ok=True)
elif name == "xcodebuild" and "-list" in args:
    print(json.dumps({{"project": {{"name": "Demo", "schemes": state["schemes"]}}}}))
elif name == "xcodebuild":
    if "archive" in args:
        Path(after("-archivePath")).mkdir(parents=True, exist_ok=True)
    destination = after("-destination")
    folder = ("Debug-iphonesimulator" if "iOS" in destination
              else "Debug-watchsimulator" if "watchOS" in destination else "Debug")
    app = Path(after("-derivedDataPath")) / "Build" / "Products" / folder / "Demo.app"
    app.mkdir(parents=True, exist_ok=True)
    if folder == "Debug":  # a Mac app: its program is this script, called Demo
        (app / "Contents" / "MacOS").mkdir(parents=True, exist_ok=True)
        shutil.copy(sys.argv[0], app / "Contents" / "MacOS" / "Demo")
    (app.parent / "DemoTests.xctest").mkdir(exist_ok=True)
    print(state.get("output", "** BUILD SUCCEEDED **"))
    sys.exit(state.get("exit", 0))
elif name == "xcrun" and args[:2] == ["simctl", "list"]:
    print(json.dumps({{"devices": state["devices"]}}))
elif name == "xcrun" and args[:2] == ["simctl", "boot"] and state.get("booted"):
    print("Unable to boot device in current state: Booted", file=sys.stderr)
    sys.exit(149)
elif name == "xcrun" and args[:2] == ["simctl", "io"]:
    Path(args[-1]).write_bytes(state["png"].encode("latin-1"))
elif name == "screencapture":
    Path(args[-1]).write_bytes(state["png"].encode("latin-1"))
elif name == "plutil":
    print("Demo" if args[1] == "CFBundleExecutable" else "com.example.demo")
elif name == "Demo":
    Path(os.environ["FAKE_LOG"]).with_name("demo.pid").write_text(str(os.getpid()))
    if state.get("app_crashes"):
        sys.exit("Fatal error: no window")
    time.sleep(60)  # a running app, until it is stopped
"""

DEVICES = {
    "com.apple.CoreSimulator.SimRuntime.iOS-17-5": [
        {"name": "iPhone 15", "udid": "OLD1", "state": "Shutdown", "isAvailable": True},
    ],
    "com.apple.CoreSimulator.SimRuntime.iOS-18-2": [
        {"name": "iPhone 16", "udid": "PHONE", "state": "Shutdown", "isAvailable": True},
        {"name": "iPad Air 11-inch (M2)", "udid": "TABLET", "state": "Shutdown",
         "isAvailable": True},
    ],
    "com.apple.CoreSimulator.SimRuntime.watchOS-11-2": [
        {"name": "Apple Watch Series 9 (45mm)", "udid": "WATCH", "state": "Shutdown",
         "isAvailable": True},
    ],
}  # fmt: skip


class Fakes:
    """The fake tools on PATH, their log and the answers they give."""

    def __init__(self, folder: Path, monkeypatch: pytest.MonkeyPatch, *, skip: str = "") -> None:
        self.bin, self.log, self.state_file = (
            folder / "bin",
            folder / "log.jsonl",
            folder / "s.json",
        )
        self.bin.mkdir()
        for tool in TOOLS:
            if tool != skip:
                (self.bin / tool).write_text(FAKE.format(python=sys.executable))
                (self.bin / tool).chmod(0o755)
        self.log.write_text("")
        self.set(schemes=["Demo_iOS", "Demo_macOS", "Demo_watchOS"], devices=DEVICES,
                 png=PNG.decode("latin-1"))  # fmt: skip
        monkeypatch.setenv("PATH", str(self.bin))
        monkeypatch.setenv("FAKE_LOG", str(self.log))
        monkeypatch.setenv("FAKE_STATE", str(self.state_file))

    def set(self, **state: Any) -> None:
        current = json.loads(self.state_file.read_text()) if self.state_file.exists() else {}
        self.state_file.write_text(json.dumps({**current, **state}))

    def calls(self, tool: str | None = None) -> list[list[str]]:
        found = [json.loads(line) for line in self.log.read_text().splitlines()]
        return [c[1:] for c in found if tool is None or c[0] == tool]


@pytest.fixture
def project(tmp_path: Path) -> Path:
    root = tmp_path / "Demo"
    root.mkdir()
    (root / "project.yml").write_text("name: Demo\n")
    return root


def builder(root: Path, tmp_path: Path, **cfg: Any) -> XcodeBuilder:
    return XcodeBuilder(root, AppleConfig(**cfg), data_dir=tmp_path / "data", launch_wait_s=0)


async def test_a_build_generates_the_project_and_picks_the_platforms_scheme(
    project: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fakes = Fakes(tmp_path, monkeypatch)
    result = await builder(project, tmp_path).build("ios", "build")
    assert result.ok and result.scheme == "Demo_iOS" and not result.issues
    assert fakes.calls("xcodegen") == [["generate", "--spec", "project.yml", "--quiet"]]
    listing, build = fakes.calls("xcodebuild")
    assert listing == ["-list", "-json", "-project", "Demo.xcodeproj"]
    assert build[:6] == ["-project", "Demo.xcodeproj", "-scheme", "Demo_iOS", "-destination",
                         "generic/platform=iOS Simulator"]  # fmt: skip
    assert build[-2:] == ["build", "CODE_SIGNING_ALLOWED=NO"]
    mac = await builder(project, tmp_path).build("macos", "build")
    assert mac.scheme == "Demo_macOS" and "platform=macOS" in fakes.calls("xcodebuild")[-1]


async def test_a_failed_build_lists_each_message_once(
    project: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fakes = Fakes(tmp_path, monkeypatch)
    source = project / "Sources" / "ContentView.swift"
    error = f"{source}:12:5: error: cannot find 'foo' in scope"
    output = "\n".join([error, f"{source}:3:9: warning: variable 'x' was never used", error,
                        "error: Signing for \"Demo\" requires a development team.",
                        "** BUILD FAILED **"])  # fmt: skip
    fakes.set(output=output, exit=65)
    result = await builder(project, tmp_path).build("ipados", "build", scheme="Demo_iOS")
    assert not result.ok and result.platform == "ipados"
    assert [(i.severity, i.file, i.line) for i in result.issues] == [
        ("error", str(source), 12), ("warning", str(source), 3), ("error", "", 0),
    ]  # fmt: skip
    assert result.issues[0].message == "cannot find 'foo' in scope"
    assert result.log_tail.endswith("** BUILD FAILED **")
    assert fakes.calls("xcodebuild")[0][0] != "-list"  # a given scheme needs no listing


async def test_tests_run_on_a_simulator_of_the_right_family(
    project: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fakes = Fakes(tmp_path, monkeypatch)
    fakes.set(output="Executed 3 tests, with 1 failure (0 unexpected) in 0.2 s\n** TEST FAILED **",
              exit=65)  # fmt: skip
    # The configured Series 10 is not installed: the newest Apple Watch there is takes its place.
    result = await builder(project, tmp_path).build("watchos", "test")
    assert (result.tests_run, result.tests_failed, result.scheme) == (3, 1, "Demo_watchOS")
    test = fakes.calls("xcodebuild")[-1]
    assert "platform=watchOS Simulator,id=WATCH" in test and test[-2] == "test"
    await builder(project, tmp_path).build("ios", "test")
    assert "platform=iOS Simulator,id=PHONE" in fakes.calls("xcodebuild")[-1]  # newest runtime


async def test_an_archive_is_unsigned_and_kept_on_the_builder(
    project: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fakes = Fakes(tmp_path, monkeypatch)
    result = await builder(project, tmp_path).build("ios", "archive")
    archive = tmp_path / "data" / "archives" / "Demo_iOS-ios.xcarchive"
    assert result.ok and result.artifact == str(archive) and archive.is_dir()
    call = fakes.calls("xcodebuild")[-1]
    assert "generic/platform=iOS" in call and "archive" in call
    assert (
        call[call.index("-archivePath") + 1] == str(archive) and "CODE_SIGNING_ALLOWED=NO" in call
    )


async def test_a_simulator_screenshot(
    project: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fakes = Fakes(tmp_path, monkeypatch)
    fakes.set(booted=True)  # already running: that is fine
    screen = await builder(project, tmp_path).screenshot("ipados", dark=True)
    assert screen.device == "iPad Air 11-inch (M2)" and screen.dark
    assert base64.b64decode(screen.image.data_b64) == PNG and screen.image.media_type == "image/png"
    simctl = [c[1:] for c in fakes.calls("xcrun")]
    app = str(tmp_path / "data" / "DerivedData" / "Build" / "Products" / "Debug-iphonesimulator"
              / "Demo.app")  # fmt: skip
    assert ["boot", "TABLET"] in simctl and ["bootstatus", "TABLET", "-b"] in simctl
    assert ["ui", "TABLET", "appearance", "dark"] in simctl
    assert ["install", "TABLET", app] in simctl
    assert ["launch", "--terminate-running-process", "TABLET", "com.example.demo"] in simctl
    assert any(c[:3] == ["io", "TABLET", "screenshot"] for c in simctl)
    # Built for any simulator first (Xcode need not know the device), then booted.
    order = [json.loads(line)[:3] for line in fakes.log.read_text().splitlines()]
    build = next(i for i, c in enumerate(order) if c[0] == "xcodebuild" and c[1] != "-list")
    assert build < order.index(["xcrun", "simctl", "boot"])
    assert "generic/platform=iOS Simulator" in fakes.calls("xcodebuild")[-1]


async def test_forge_shuts_down_the_simulators_it_started(
    project: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fakes = Fakes(tmp_path, monkeypatch)
    apple = builder(project, tmp_path)
    await apple.screenshot("ios")
    await apple.screenshot("ipados")  # one simulator at a time: the iPhone goes first
    shutdowns = [c[2] for c in fakes.calls("xcrun") if c[:2] == ["simctl", "shutdown"]]
    assert shutdowns == ["PHONE"]
    await apple.screenshot("ipados", dark=True)  # the same device stays up
    await apple.close()
    shutdowns = [c[2] for c in fakes.calls("xcrun") if c[:2] == ["simctl", "shutdown"]]
    assert shutdowns == ["PHONE", "TABLET"]
    fakes.set(booted=True)  # running before Forge came: Forge leaves it running
    other = builder(project, tmp_path)
    await other.screenshot("watchos")
    await other.close()
    assert [c[2] for c in fakes.calls("xcrun") if c[:2] == ["simctl", "shutdown"]] == [
        "PHONE",
        "TABLET",
    ]


async def test_a_watch_screenshot_is_always_dark(
    project: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fakes = Fakes(tmp_path, monkeypatch)
    screen = await builder(project, tmp_path).screenshot("watchos")
    assert screen.device == "Apple Watch Series 9 (45mm)" and screen.dark
    assert not any(c[1] == "ui" for c in fakes.calls("xcrun"))  # it has no light appearance


async def test_a_mac_screenshot_starts_the_app_in_its_appearance_and_stops_it(
    project: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fakes = Fakes(tmp_path, monkeypatch)
    # Time for the fake app to start and log its arguments before the picture is taken.
    patient = XcodeBuilder(project, AppleConfig(), data_dir=tmp_path / "data", launch_wait_s=2)
    screen = await patient.screenshot("macos", dark=True)
    assert screen.device == "Mac" and screen.dark
    assert base64.b64decode(screen.image.data_b64) == PNG and fakes.calls("screencapture")
    assert fakes.calls("Demo") == [["-AppleInterfaceStyle", "Dark"]]
    pid = int((tmp_path / "demo.pid").read_text())
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)  # stopped and reaped once the picture is taken


async def test_a_mac_app_that_quits_at_once_is_an_error(
    project: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fakes = Fakes(tmp_path, monkeypatch)
    fakes.set(app_crashes=True)
    crashing = XcodeBuilder(project, AppleConfig(), data_dir=tmp_path / "data", launch_wait_s=10)
    with pytest.raises(AppleBuildError, match="quit right after it started") as quit_early:
        await crashing.screenshot("macos")
    assert "Fatal error: no window" in str(quit_early.value)
    assert not fakes.calls("screencapture")


async def test_what_cannot_work_is_said_plainly(
    project: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    Fakes(tmp_path, monkeypatch, skip="xcodegen")
    with pytest.raises(AppleBuildError) as missing:
        await builder(project, tmp_path).build("ios", "build")
    assert "xcodegen" in str(missing.value) and "brew install xcodegen" in missing.value.hint
    (project / "project.yml").unlink()
    with pytest.raises(AppleBuildError, match="no Xcode project"):
        await builder(project, tmp_path).build("ios", "build")


async def test_unclear_schemes_and_devices_are_errors(
    project: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fakes = Fakes(tmp_path, monkeypatch)
    fakes.set(schemes=["Alpha", "Beta"])
    with pytest.raises(AppleBuildError, match="Alpha, Beta") as unclear:
        await builder(project, tmp_path).build("ios", "build")
    assert "scheme" in unclear.value.hint
    fakes.set(devices={})
    with pytest.raises(AppleBuildError, match="no iPhone simulator"):
        await builder(project, tmp_path).screenshot("ios", device="iPhone 99")


async def test_without_xcode_the_builder_says_so(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "Plain"
    (root / "Demo.xcodeproj").mkdir(parents=True)
    monkeypatch.setenv("PATH", str(tmp_path))  # no Apple tools at all
    with pytest.raises(AppleBuildError, match="xcodebuild") as missing:
        await builder(root, tmp_path).build("macos", "build", scheme="Demo")
    assert "Xcode" in missing.value.hint


def test_the_default_data_folder_is_per_project(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FORGE_HOME", str(tmp_path))
    one = XcodeBuilder(tmp_path / "a", AppleConfig())
    two = XcodeBuilder(tmp_path / "b", AppleConfig())
    assert one.data != two.data and one.data.parent == two.data.parent == tmp_path / "apple"
