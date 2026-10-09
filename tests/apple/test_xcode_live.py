"""The app from `forge apple new`, built, tested and photographed with a real Xcode.

Marked `apple` and skipped by default: it needs a Mac with Xcode and XcodeGen (`uv run pytest -m
apple -q`). CI runs it on a macOS runner; with APPLE_SHOTS_DIR set, the screenshots are saved
there for a person to look at.
"""

import base64
import os
import sys
from pathlib import Path

import pytest

from forge.apple_template import write_app
from forge.config import AppleConfig
from forge.local.xcode_builder import XcodeBuilder
from forge.ports import ApplePlatform
from forge.runtime.apple import build_report

pytestmark = [
    pytest.mark.apple,
    pytest.mark.skipif(sys.platform != "darwin", reason="Xcode runs only on macOS"),
]


@pytest.fixture(scope="module")
def app(tmp_path_factory: pytest.TempPathFactory) -> Path:
    root = tmp_path_factory.mktemp("apple") / "Tally"
    write_app(root, "Tally", "com.example.tally")
    return root


def builder(app: Path) -> XcodeBuilder:
    # One DerivedData for the module: later builds reuse what earlier ones made. A step that
    # hangs ends after 15 minutes instead of using up the CI job.
    cfg = AppleConfig(timeout_s=900)
    return XcodeBuilder(app, cfg, data_dir=app.parent / "data", launch_wait_s=8)


@pytest.mark.parametrize("platform", ["ios", "macos", "watchos"])
async def test_the_template_builds_without_warnings(app: Path, platform: ApplePlatform) -> None:
    result = await builder(app).build(platform, "build")
    assert result.ok and not result.issues, build_report(result, app)


@pytest.mark.parametrize("platform", ["ios", "macos"])
async def test_its_unit_tests_pass(app: Path, platform: ApplePlatform) -> None:
    result = await builder(app).build(platform, "test")
    assert result.ok, build_report(result, app)
    assert (result.tests_run, result.tests_failed) == (2, 0)


async def test_the_iphone_archive_carries_the_watch_app(app: Path) -> None:
    result = await builder(app).build("ios", "archive")
    assert result.ok, build_report(result, app)
    apps = sorted((Path(result.artifact) / "Products" / "Applications").glob("*.app"))
    assert [a.name for a in apps] == ["Tally.app"]
    assert (apps[0] / "Watch" / "TallyWatch.app").is_dir()


@pytest.mark.parametrize(
    ("platform", "dark"), [("ios", False), ("ipados", True), ("watchos", True), ("macos", False)]
)
async def test_screenshots_of_every_device(app: Path, platform: ApplePlatform, dark: bool) -> None:
    apple = builder(app)
    try:
        screen = await apple.screenshot(platform, dark=dark)
    finally:
        await apple.close()  # its simulator goes down before the next device starts
    data = base64.b64decode(screen.image.data_b64)
    assert data[:8] == b"\x89PNG\r\n\x1a\n" and len(data) > 2_000, screen.device
    folder = os.environ.get("APPLE_SHOTS_DIR")
    if folder:
        Path(folder).mkdir(parents=True, exist_ok=True)
        mode = "dark" if screen.dark else "light"
        (Path(folder) / f"{platform}-{mode}.png").write_bytes(data)
