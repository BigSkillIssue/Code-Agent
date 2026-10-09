"""AppleBuilder on this Mac: XcodeGen, xcodebuild and simctl (docs/CONTRACTS.md, Ports).

Builds go to a folder of their own outside the project (DerivedData, archives), and nothing is
signed here: simulators need no signature, and archives are signed later, where the signing
keys are.
"""

import asyncio
import base64
import contextlib
import hashlib
import json
import re
from pathlib import Path
from typing import Any, Literal

from forge.config import AppleConfig, forge_home
from forge.ports import (
    AppleAction,
    AppleBuildError,
    AppleBuildResult,
    AppleIssue,
    ApplePlatform,
    AppleScreen,
)
from forge.providers.base import ImagePart

XCODE_HINT = "install Xcode (App Store) and run `sudo xcode-select -s /Applications/Xcode.app`"
GENERIC = {
    "ios": "generic/platform=iOS Simulator",
    "ipados": "generic/platform=iOS Simulator",
    "watchos": "generic/platform=watchOS Simulator",
    "macos": "platform=macOS",
}
ARCHIVE = {
    "ios": "generic/platform=iOS",
    "ipados": "generic/platform=iOS",
    "watchos": "generic/platform=watchOS",
    "macos": "generic/platform=macOS",
}
SIMULATOR = {"ios": "iOS Simulator", "ipados": "iOS Simulator", "watchos": "watchOS Simulator"}
RUNTIME = {"ios": "iOS", "ipados": "iOS", "watchos": "watchOS"}
FAMILY = {"ios": "iPhone", "ipados": "iPad", "watchos": "Apple Watch"}
PRODUCTS = {"ios": "Debug-iphonesimulator", "ipados": "Debug-iphonesimulator",
            "watchos": "Debug-watchsimulator", "macos": "Debug"}  # fmt: skip
SCHEME_WORDS = {"ios": ("ios", "iphone"), "ipados": ("ipad", "ios"), "macos": ("macos", "mac"),
                "watchos": ("watch",)}  # fmt: skip
ISSUE = re.compile(
    r"^(?P<file>/[^:\n]+):(?P<line>\d+)(?::\d+)?: (?P<sev>error|warning): (?P<msg>.+)$", re.M
)
PLAIN_ISSUE = re.compile(r"^(?:xcodebuild: )?(?P<sev>error|warning): (?P<msg>.+)$", re.M)
TESTS = re.compile(r"Executed (\d+) tests?, with (\d+) failures?")
LOG_TAIL_LINES = 60


class XcodeBuilder:
    """Builds the project at `root` with the Xcode of this Mac."""

    def __init__(
        self,
        root: Path,
        cfg: AppleConfig,
        *,
        data_dir: Path | None = None,
        launch_wait_s: float = 4.0,
    ) -> None:
        self.root = root
        self.cfg = cfg
        key = hashlib.sha256(str(root).encode()).hexdigest()[:12]
        self.data = data_dir or forge_home() / "apple" / key  # DerivedData, archives, shots
        self.launch_wait_s = launch_wait_s

    async def build(
        self, platform: ApplePlatform, action: AppleAction, scheme: str | None = None
    ) -> AppleBuildResult:
        """Build, test or archive for one platform."""
        project = await self.project()
        scheme = scheme or await self.scheme(project, platform)
        argv = ["xcodebuild", *project, "-scheme", scheme, "-destination"]
        artifact = ""
        if action == "archive":
            archive = self.data / "archives" / f"{scheme}-{platform}.xcarchive"
            argv += [ARCHIVE[platform], "-derivedDataPath", str(self.derived), "archive",
                     "-archivePath", str(archive)]  # fmt: skip
            artifact = str(archive)
        else:
            simulated = action == "test" and platform in SIMULATOR
            device = await self.device(platform, None) if simulated else None
            destination = self.destination(platform, device[0]) if device else GENERIC[platform]
            argv += [destination, "-derivedDataPath", str(self.derived), action]
        code, out = await self.run([*argv, "CODE_SIGNING_ALLOWED=NO"], self.cfg.timeout_s)
        return parse_build(out, code == 0, platform, action, scheme, artifact if code == 0 else "")

    async def screenshot(
        self, platform: ApplePlatform, device: str | None = None, dark: bool = False
    ) -> AppleScreen:
        """Build the app, start it and take a picture of the device's screen (or the Mac's)."""
        if platform == "macos":
            return await self.mac_screenshot(dark)
        udid, name = await self.device(platform, device)
        await self.run(["xcrun", "simctl", "boot", udid], 120)  # fails harmlessly when running
        await self.checked(["xcrun", "simctl", "bootstatus", udid, "-b"], 300)
        app = await self.built_app(platform, self.destination(platform, udid))
        bundle = await self.bundle_id(app / "Info.plist")
        await self.checked(
            ["xcrun", "simctl", "ui", udid, "appearance", "dark" if dark else "light"]
        )
        await self.checked(["xcrun", "simctl", "install", udid, str(app)], 300)
        await self.checked(
            ["xcrun", "simctl", "launch", "--terminate-running-process", udid, bundle], 120
        )
        await asyncio.sleep(self.launch_wait_s)
        shot = self.data / "shots" / f"{platform}-{udid}.png"
        shot.parent.mkdir(parents=True, exist_ok=True)
        await self.checked(["xcrun", "simctl", "io", udid, "screenshot", "--type=png", str(shot)])
        return AppleScreen(platform=platform, device=name, dark=dark, image=png(shot))

    async def mac_screenshot(self, dark: bool) -> AppleScreen:
        """Start the Mac app and take a picture of the screen."""
        app = await self.built_app("macos", GENERIC["macos"])
        bundle = await self.bundle_id(app / "Contents" / "Info.plist")
        await self.checked(["open", "-n", str(app)], 60)
        await asyncio.sleep(self.launch_wait_s)
        shot = self.data / "shots" / "macos.png"
        shot.parent.mkdir(parents=True, exist_ok=True)
        try:
            await self.checked(["screencapture", "-x", str(shot)], 60)
        finally:
            await self.run(["osascript", "-e", f'tell application id "{bundle}" to quit'], 30)
        return AppleScreen(platform="macos", device="Mac", dark=dark, image=png(shot))

    async def close(self) -> None:
        """Nothing stays running between calls."""

    # project, scheme, device -----------------------------------------------------------

    @property
    def derived(self) -> Path:
        return self.data / "DerivedData"

    async def project(self) -> list[str]:
        """`-project`/`-workspace` arguments, after XcodeGen made the project from project.yml."""
        if (self.root / "project.yml").is_file():
            await self.checked(["xcodegen", "generate", "--spec", "project.yml", "--quiet"])
        workspaces = sorted(self.root.glob("*.xcworkspace"))
        if workspaces:
            return ["-workspace", workspaces[0].name]
        projects = sorted(self.root.glob("*.xcodeproj"))
        if projects:
            return ["-project", projects[0].name]
        if (self.root / "Package.swift").is_file():
            return []
        raise AppleBuildError(
            "no Xcode project here", hint="add a project.yml (XcodeGen) or an .xcodeproj"
        )

    async def scheme(self, project: list[str], platform: ApplePlatform) -> str:
        """The scheme for the platform: by its name, or the only one there is."""
        listing = json.loads(await self.checked(["xcodebuild", "-list", "-json", *project]) or "{}")
        found = (listing.get("workspace") or listing.get("project") or {}).get("schemes", [])
        schemes = [str(s) for s in found]
        for word in SCHEME_WORDS[platform]:
            matching = [s for s in schemes if word in s.lower()]
            if matching:
                return matching[0]
        if len(schemes) == 1:
            return schemes[0]
        raise AppleBuildError(
            f"which scheme builds {platform}? found: {', '.join(schemes) or 'none'}",
            hint="pass scheme, or name schemes after their platform (App_iOS, App_macOS)",
        )

    async def device(self, platform: ApplePlatform, wanted: str | None) -> tuple[str, str]:
        """(udid, name) of the simulator: the wanted one, else the newest of its family."""
        if platform not in SIMULATOR:
            raise AppleBuildError(f"{platform} apps run on the Mac itself, not in a simulator")
        out = await self.checked(["xcrun", "simctl", "list", "devices", "available", "--json"])
        devices = json.loads(out or "{}").get("devices", {})
        name = wanted or self.cfg.devices.get(platform, "")
        candidates = [
            (runtime_version(runtime), d)
            for runtime, listed in devices.items()
            if f".{RUNTIME[platform]}-" in runtime
            for d in listed
            if isinstance(d, dict) and d.get("isAvailable", True)
        ]
        exact = [d for _, d in candidates if d.get("name") == name]
        family = [d for _, d in sorted(candidates, key=lambda c: c[0], reverse=True)
                  if str(d.get("name", "")).startswith(FAMILY[platform])]  # fmt: skip
        chosen = exact or ([] if wanted else family)
        if not chosen:
            raise AppleBuildError(
                f"no {FAMILY[platform]} simulator called {name!r}" if wanted else
                f"no {FAMILY[platform]} simulator is installed",
                hint="xcrun simctl list devices available; Xcode > Settings > Platforms",
            )  # fmt: skip
        return str(chosen[0]["udid"]), str(chosen[0]["name"])

    def destination(self, platform: ApplePlatform, udid: str) -> str:
        return f"platform={SIMULATOR[platform]},id={udid}"

    async def built_app(self, platform: ApplePlatform, destination: str) -> Path:
        """Build for the destination and find the app it made."""
        project = await self.project()
        scheme = await self.scheme(project, platform)
        argv = ["xcodebuild", *project, "-scheme", scheme, "-destination", destination,
                "-derivedDataPath", str(self.derived), "build",
                "CODE_SIGNING_ALLOWED=NO"]  # fmt: skip
        code, out = await self.run(argv, self.cfg.timeout_s)
        if code != 0:
            result = parse_build(out, False, platform, "build", scheme, "")
            first = next((i.message for i in result.issues if i.severity == "error"), "")
            raise AppleBuildError(f"the build failed: {first or 'see apple_build'}",
                                  hint="fix the build with apple_build first")  # fmt: skip
        products = self.derived / "Build" / "Products" / PRODUCTS[platform]
        apps = sorted(products.glob("*.app"))
        if not apps:
            raise AppleBuildError(f"the build made no app in {products}")
        return apps[0]

    async def bundle_id(self, plist: Path) -> str:
        out = await self.checked(["plutil", "-extract", "CFBundleIdentifier", "raw", "-o", "-",
                                  str(plist)])  # fmt: skip
        return out.strip()

    # running tools ------------------------------------------------------------------------

    async def run(self, argv: list[str], timeout: float = 120) -> tuple[int, str]:
        """Exit code and output (stdout and stderr together) of one command in the project."""
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv, cwd=self.root, stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
            )  # fmt: skip
        except FileNotFoundError:
            hint = "brew install xcodegen" if argv[0] == "xcodegen" else XCODE_HINT
            raise AppleBuildError(f"{argv[0]} not found", hint=hint) from None
        try:
            out, _ = await asyncio.wait_for(proc.communicate(), timeout)
        except TimeoutError:
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
            await proc.wait()
            raise AppleBuildError(f"{argv[0]} took longer than {timeout:.0f} s") from None
        return (proc.returncode if proc.returncode is not None else -1), out.decode(
            "utf-8", "replace"
        )

    async def checked(self, argv: list[str], timeout: float = 120) -> str:
        """Output of a command that has to work."""
        code, out = await self.run(argv, timeout)
        if code != 0:
            last = out.strip().splitlines()[-1] if out.strip() else f"exit code {code}"
            raise AppleBuildError(f"{' '.join(argv[:3])} failed: {last[:300]}")
        return out


def parse_build(
    out: str, ok: bool, platform: ApplePlatform, action: AppleAction, scheme: str, artifact: str
) -> AppleBuildResult:
    """The messages (each once, in order), test counts and log end of an xcodebuild run."""
    issues: list[AppleIssue] = []
    seen: set[tuple[Any, ...]] = set()
    for line in out.splitlines():
        match = ISSUE.match(line)
        if match:
            issue = AppleIssue(severity=severity(match["sev"]), message=match["msg"].strip(),
                               file=match["file"], line=int(match["line"]))  # fmt: skip
        elif plain := PLAIN_ISSUE.match(line):
            issue = AppleIssue(severity=severity(plain["sev"]), message=plain["msg"].strip())
        else:
            continue
        key = (issue.severity, issue.file, issue.line, issue.message)
        if key not in seen:
            seen.add(key)
            issues.append(issue)
    counts = TESTS.findall(out)
    run, failed = (int(counts[-1][0]), int(counts[-1][1])) if counts else (0, 0)
    tail = "\n".join(out.strip().splitlines()[-LOG_TAIL_LINES:])
    return AppleBuildResult(
        ok=ok, platform=platform, action=action, scheme=scheme, issues=issues,
        tests_run=run, tests_failed=failed, log_tail=tail, artifact=artifact,
    )  # fmt: skip


def severity(word: str) -> Literal["error", "warning"]:
    return "error" if word == "error" else "warning"


def runtime_version(runtime: str) -> tuple[int, ...]:
    """`...SimRuntime.iOS-18-2` -> (18, 2), for picking the newest."""
    numbers = re.findall(r"\d+", runtime.rsplit(".", 1)[-1])
    return tuple(int(n) for n in numbers)


def png(path: Path) -> ImagePart:
    """A PNG file as an image part."""
    return ImagePart(media_type="image/png", data_b64=base64.b64encode(path.read_bytes()).decode())
