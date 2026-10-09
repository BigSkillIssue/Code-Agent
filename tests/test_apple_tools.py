"""Apple builds through the AppleBuilder port (a fake builder here): tools, reports, screenshots."""

from pathlib import Path
from typing import Any

from forge.agent import prompt_slots
from forge.config import AppleConfig, ForgeConfig
from forge.ctx import Ctx
from forge.ports import (
    AppleAction,
    AppleBuildError,
    AppleBuildResult,
    AppleIssue,
    ApplePlatform,
    AppleScreen,
)
from forge.providers.base import ImagePart, ToolCall, ToolResult
from forge.runtime.apple import is_apple_project
from forge.tools import agent_tools, call_tool
from support import make_ctx

SHOT = ImagePart(media_type="image/png", data_b64="iVBORw0KGgo=")


class FakeAppleBuilder:
    """Records calls; every build ends as `result`, every screenshot shows SHOT."""

    def __init__(self, result: AppleBuildResult | None = None, fail: str = "") -> None:
        self.calls: list[tuple[Any, ...]] = []
        self.result = result or AppleBuildResult(
            ok=True, platform="ios", action="build", scheme="Demo_iOS"
        )
        self.fail = fail

    async def build(
        self, platform: ApplePlatform, action: AppleAction, scheme: str | None = None
    ) -> AppleBuildResult:
        self.calls.append(("build", platform, action, scheme))
        if self.fail:
            raise AppleBuildError(self.fail, hint="install Xcode from the App Store")
        return self.result

    async def screenshot(
        self, platform: ApplePlatform, device: str | None = None, dark: bool = False
    ) -> AppleScreen:
        self.calls.append(("screenshot", platform, device, dark))
        return AppleScreen(platform=platform, device=device or "iPhone 16", dark=dark, image=SHOT)

    async def close(self) -> None:
        self.calls.append(("close",))


def apple_ctx(root: Path, builder: FakeAppleBuilder | None = None, **cfg: Any) -> Ctx:
    ctx = make_ctx(root, cfg=ForgeConfig(**cfg))
    ctx.state.apple = builder or FakeAppleBuilder()
    return ctx


async def run(ctx: Ctx, name: str, **arguments: Any) -> ToolResult:
    return await call_tool(ctx, ToolCall(id="c1", name=name, arguments=arguments))


def test_apple_tools_need_a_builder_and_a_role_that_changes_things(ctx: Ctx) -> None:
    assert not any(t.group == "apple" for t in agent_tools(ctx, "coder"))
    ctx.state.apple = FakeAppleBuilder()
    assert {"apple_build", "apple_screenshot"} <= {t.name for t in agent_tools(ctx, "coder")}
    for role in ("reviewer", "explore", "planner", "browser"):
        assert not any(t.group == "apple" for t in agent_tools(ctx, role)), role


async def test_a_green_build_is_reported_briefly(tmp_project: Path) -> None:
    builder = FakeAppleBuilder()
    ctx = apple_ctx(tmp_project, builder)
    result = await run(ctx, "apple_build", platform="ios")
    assert result.ok and builder.calls == [("build", "ios", "build", None)]
    assert result.text.splitlines()[0] == "build for ios (scheme Demo_iOS): succeeded"


async def test_a_failed_build_lists_errors_tests_and_the_log_end(tmp_project: Path) -> None:
    source = tmp_project / "Sources" / "App.swift"
    failed = AppleBuildResult(
        ok=False, platform="macos", action="test", scheme="Demo_macOS",
        issues=[
            AppleIssue(severity="error", message="cannot find 'foo' in scope", file=str(source),
                       line=12),
            AppleIssue(severity="warning", message="variable 'x' was never used", file=str(source),
                       line=3),
            AppleIssue(severity="error", message="linker command failed"),
        ],
        tests_run=4, tests_failed=1, log_tail="** TEST FAILED **",
    )  # fmt: skip
    ctx = apple_ctx(tmp_project, FakeAppleBuilder(failed))
    result = await run(ctx, "apple_build", platform="macos", action="test", scheme="Demo_macOS")
    assert not result.ok and result.code == "exit_nonzero"
    lines = result.text.splitlines()
    assert lines[0] == "test for macos (scheme Demo_macOS): failed"
    assert "tests: 4 run, 1 failed" in lines
    assert "Sources/App.swift:12: error: cannot find 'foo' in scope" in lines  # relative to root
    assert "error: linker command failed" in lines
    assert "Sources/App.swift:3: warning: variable 'x' was never used" in lines
    assert lines[-1] == "** TEST FAILED **"


async def test_an_archive_says_where_it_is(tmp_project: Path) -> None:
    archived = AppleBuildResult(ok=True, platform="ios", action="archive", scheme="Demo_iOS",
                                artifact="/builds/Demo_iOS-ios.xcarchive")  # fmt: skip
    ctx = apple_ctx(tmp_project, FakeAppleBuilder(archived))
    result = await run(ctx, "apple_build", platform="ios", action="archive")
    assert result.ok and "archive: /builds/Demo_iOS-ios.xcarchive" in result.text


async def test_builder_problems_become_tool_errors(tmp_project: Path) -> None:
    ctx = apple_ctx(tmp_project, FakeAppleBuilder(fail="xcodebuild is not installed"))
    result = await run(ctx, "apple_build", platform="ios")
    assert not result.ok and result.code == "unsupported"
    assert "xcodebuild is not installed" in result.text and "App Store" in result.text


async def test_unknown_platforms_are_refused(tmp_project: Path) -> None:
    builder = FakeAppleBuilder()
    ctx = apple_ctx(tmp_project, builder)
    result = await run(ctx, "apple_build", platform="android")
    assert result.code == "invalid_args" and not builder.calls


async def test_screenshots_are_shown_kept_and_limited(tmp_project: Path) -> None:
    ctx = apple_ctx(tmp_project, apple=AppleConfig(max_screenshots=2))
    first = await run(ctx, "apple_screenshot", platform="ipados", device="iPad Air", dark=True)
    assert first.ok and first.images == [SHOT]
    assert "iPad Air" in first.text and "dark" in first.text
    second = await run(ctx, "apple_screenshot", platform="ios")
    assert second.ok and "iPhone 16" in second.text and "light" in second.text
    third = await run(ctx, "apple_screenshot", platform="watchos")
    assert not third.ok and not third.images and "limit" in third.text
    kept = [(s.platform, s.device, s.dark) for s in ctx.state.apple_screens]
    assert kept == [("ipados", "iPad Air", True), ("ios", "iPhone 16", False)]


async def test_a_newer_screenshot_of_the_same_device_replaces_the_older(tmp_project: Path) -> None:
    ctx = apple_ctx(tmp_project)
    for _ in range(3):
        await run(ctx, "apple_screenshot", platform="ios")
    assert len(ctx.state.apple_screens) == 1  # the reviewer sees each device once


def test_apple_projects_are_recognized(tmp_path: Path) -> None:
    assert not is_apple_project(tmp_path)
    (tmp_path / "project.yml").write_text("name: Demo\n")
    assert is_apple_project(tmp_path)
    other = tmp_path / "other"
    (other / "Demo.xcodeproj").mkdir(parents=True)
    assert is_apple_project(other)


def test_apple_projects_and_builders_get_apple_guidance(tmp_project: Path) -> None:
    ctx = make_ctx(tmp_project)
    assert prompt_slots(ctx)["apple"] == ""
    ctx.state.apple = FakeAppleBuilder()
    assert "XcodeGen" in prompt_slots(ctx)["apple"]
    plain = make_ctx(tmp_project)
    (tmp_project / "Demo.xcworkspace").mkdir()
    assert "SwiftUI" in prompt_slots(plain)["apple"]
