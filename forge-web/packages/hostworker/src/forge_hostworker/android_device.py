"""An Android emulator for Forge's agent (W61): the product's Android app, built from its packed
source as a debug APK, installed and started on an emulator, and driven through `adb`.

The emulator needs KVM, which gVisor does not pass on, so its container runs on Docker's
default runtime: all capabilities dropped, no new privileges, an unprivileged user (in the
group of /dev/kvm), a read-only image, limits, and the build network (DNS, HTTP, HTTPS; no
private network, so the app reaches its staging server over the internet like a phone). The
app's own code runs inside the Android VM, a second wall. The APK is built like every build:
in a gVisor container without secrets.
"""

import asyncio
import os
import re
import shlex
import shutil
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from forge_hostworker.android import FOLDER
from forge_hostworker.build import SourceError, hand_over, unpack
from forge_hostworker.device_wire import DeviceAction, DeviceAnswer, DeviceOffer
from forge_hostworker.docker import DockerResult, hardened
from forge_hostworker.firewall import BUILD_NETWORK
from forge_hostworker.screens import encoded, png_size

if TYPE_CHECKING:
    from forge_hostworker.runner import HostRunner

KVM = Path("/dev/kvm")
APK = Path("app/build/outputs/apk/debug/app-debug.apk")
BUILD_TIMEOUT_S = 1800
BOOT_TIMEOUT_S = 600
ADB_TIMEOUT_S = 60
SETTLE_S = 0.8  # the screen after an action, once animations have run
MEMORY, CPUS, PIDS = "4g", "4", "2048"
KEYCODES = {"back": "4", "home": "3", "enter": "66", "tab": "61", "escape": "111",
            "menu": "82", "delete": "67"}  # fmt: skip
BOUNDS = re.compile(r"^\[(\d+),(\d+)\]\[(\d+),(\d+)\]$")
UI_DUMP = "/sdcard/forge-ui.xml"


class DeviceProblem(Exception):
    """The device could not do what was asked; `hint` says what may help."""

    def __init__(self, message: str, hint: str = "") -> None:
        super().__init__(message)
        self.hint = hint


@dataclass(frozen=True)
class Element:
    """One element of the screen, as uiautomator sees it."""

    kind: str
    text: str
    name: str  # content-desc, the accessibility name
    ident: str  # resource-id
    box: tuple[int, int, int, int]  # x, y, width, height
    clickable: bool

    def line(self) -> str:
        """A line for the agent."""
        label = " ".join(f"{k}={v!r}" for k, v in (("text", self.text), ("name", self.name),
                                                   ("id", self.ident)) if v)  # fmt: skip
        x, y, w, h = self.box
        tap = " tappable" if self.clickable else ""
        return f"{self.kind} {label} ({x},{y},{w},{h}){tap}"

    def center(self) -> tuple[int, int]:
        """Where a tap on it lands."""
        x, y, w, h = self.box
        return x + w // 2, y + h // 2


def elements(xml_text: str) -> list[Element]:
    """The screen's elements with text, a name, an id or a click handler."""
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return []
    found = []
    for node in root.iter("node"):
        match = BOUNDS.match(node.get("bounds", ""))
        if match is None:
            continue
        x1, y1, x2, y2 = (int(n) for n in match.groups())
        item = Element(kind=node.get("class", "").rsplit(".", 1)[-1] or "View",
                       text=node.get("text", ""), name=node.get("content-desc", ""),
                       ident=node.get("resource-id", "").rsplit("/", 1)[-1],
                       box=(x1, y1, x2 - x1, y2 - y1),
                       clickable=node.get("clickable") == "true")  # fmt: skip
        if item.text or item.name or item.ident or item.clickable:
            found.append(item)
    return found


def find(items: list[Element], target: str) -> Element | None:
    """The element a person would mean: the same text or name, else one containing it."""
    wanted = target.strip().casefold()
    exact = [
        e for e in items if wanted in (e.text.casefold(), e.name.casefold(), e.ident.casefold())
    ]
    near = [
        e for e in items if wanted and (wanted in e.text.casefold() or wanted in e.name.casefold())
    ]
    chosen = exact or near
    return min(chosen, key=lambda e: not e.clickable) if chosen else None


def typed(text: str) -> str:
    """`input text`'s argument: spaces as %s, the rest quoted for the device's shell."""
    return shlex.quote(text.replace("%", "\\%").replace(" ", "%s"))


class AndroidDevice:
    """One emulator session on this host."""

    def __init__(self, runner: "HostRunner", offer: DeviceOffer) -> None:
        self.runner = runner
        self.offer = offer
        self.work = runner.data_dir / "devices" / offer.id
        self.name = f"forge-device-{offer.id[:16]}"
        self.size = (0, 0)

    async def start(self, source: Path) -> DeviceAnswer:
        """Build the APK, boot the emulator, install and start the app: the first screen."""
        if not KVM.exists():
            raise DeviceProblem(
                "this host cannot run Android emulators: it has no /dev/kvm",
                "the admin turns on KVM (nested virtualisation on a VM)",
            )
        if not self.offer.build_image or not self.offer.device_image:
            raise DeviceProblem("the server sent no build or emulator image")
        apk = await self.build(source)
        await self.boot(apk.parent)
        package = self.offer.package or await self.installed_package()
        await self.adb("install", "-r", "/apk/" + apk.name, timeout=300)
        package = package or await self.installed_package()
        if not package:
            raise DeviceProblem("the app's application ID was not found")
        await self.adb("shell", shlex.join(["monkey", "-p", package, "-c",
                                            "android.intent.category.LAUNCHER", "1"]))  # fmt: skip
        await asyncio.sleep(3)
        return await self.screen(0, f"{package} runs on the emulator")

    async def build(self, source: Path) -> Path:
        """The debug APK of the packed source, built in gVisor without secrets."""
        await asyncio.to_thread(shutil.rmtree, self.work, True)  # nothing of an earlier try
        try:
            await asyncio.to_thread(unpack, source, self.work / "src")
        except SourceError as error:
            raise DeviceProblem(f"the source was refused: {error}") from error
        folder = self.work / "src" / FOLDER
        if not (folder / "gradlew").is_file():
            raise DeviceProblem("the product has no Android app (android/gradlew)")
        if self.runner.owner is not None:
            await asyncio.to_thread(hand_over, self.work, self.runner.owner)
        await self.runner.ensure_build_network()
        built = await self.runner.docker(*self.build_args(folder), timeout=BUILD_TIMEOUT_S)
        if built.code != 0:
            raise DeviceProblem("the Android debug build failed:\n" + built.tail()[-3000:])
        target = self.work / "apk" / "app-debug.apk"
        target.parent.mkdir(parents=True, exist_ok=True)
        apk = folder / APK
        if apk.is_symlink() or not apk.is_file():
            raise DeviceProblem("the build made no debug APK")
        await asyncio.to_thread(shutil.copyfile, apk, target)
        return target

    def build_args(self, folder: Path) -> list[str]:
        """`docker run` for the debug build."""
        return [
            "run", "--rm", *hardened(self.runner.user), "--read-only",
            "--tmpfs", "/tmp:rw,exec,size=4096m", "--network", BUILD_NETWORK,
            "--memory", "6g", "--cpus", CPUS, "--pids-limit", "4096",
            "-v", f"{folder}:/app", "-w", "/app",
            "-e", "HOME=/tmp", "-e", "GRADLE_USER_HOME=/tmp/gradle",
            "--label", f"forge.app={self.offer.app}", "--label", "forge.role=device-build",
            self.offer.build_image, "./gradlew", "--no-daemon", "assembleDebug",
        ]  # fmt: skip

    def emulator_args(self, apk_dir: Path) -> list[str]:
        """`docker run` for the emulator (Docker's own runtime: gVisor has no KVM)."""
        group = str(os.stat(KVM).st_gid)
        return [
            "run", "-d", "--rm", "--name", self.name,
            "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
            "--user", self.runner.user, "--group-add", group, "--device", str(KVM),
            "--read-only", "--tmpfs", "/tmp:rw,exec,size=8192m", "-e", "HOME=/tmp",
            "--network", BUILD_NETWORK, "--memory", MEMORY, "--cpus", CPUS, "--pids-limit", PIDS,
            "-v", f"{apk_dir}:/apk:ro",
            "--label", f"forge.app={self.offer.app}", "--label", "forge.role=device",
            self.offer.device_image,
        ]  # fmt: skip

    async def boot(self, apk_dir: Path) -> None:
        """Start the emulator and wait until Android has booted."""
        started = await self.runner.docker(*self.emulator_args(apk_dir), timeout=120)
        if started.code != 0:
            raise DeviceProblem("the emulator did not start: " + started.tail()[-500:])
        await self.adb("wait-for-device", timeout=BOOT_TIMEOUT_S)
        for _ in range(BOOT_TIMEOUT_S // 2):
            booted = await self.adb("shell", "getprop sys.boot_completed", check=False)
            if booted.out.strip() == "1":
                return
            await asyncio.sleep(2)
        raise DeviceProblem("Android did not finish booting in time")

    async def installed_package(self) -> str:
        """The one app installed besides Android's own."""
        listed = await self.adb("shell", "pm list packages -3", check=False)
        names = [line.removeprefix("package:").strip() for line in listed.out.splitlines()]
        return next((n for n in names if re.fullmatch(r"[A-Za-z][\w.]*", n)), "")

    async def adb(self, *args: str, timeout: float = ADB_TIMEOUT_S, check: bool = True,
                  out: Path | None = None) -> DockerResult:  # fmt: skip
        """`adb` in the emulator's container."""
        done = await self.runner.docker("exec", self.name, "adb", *args, timeout=timeout, out=out)
        if check and done.code != 0:
            raise DeviceProblem(f"adb {args[0]} failed: {done.tail()[-500:]}")
        return done

    # ------------------------------------------------------------------ the agent's actions

    async def act(self, action: DeviceAction) -> DeviceAnswer:
        """Do one action and show the screen afterwards (`read`: the elements instead)."""
        if action.kind == "read":
            items = await self.ui()
            text = "\n".join(e.line() for e in items) or "no elements with text on this screen"
            return DeviceAnswer(seq=action.seq, ok=True, text=text[:50_000])
        note = await self.do(action)
        await asyncio.sleep(SETTLE_S)
        return await self.screen(action.seq, note)

    async def do(self, action: DeviceAction) -> str:
        """The action itself; what it did, in a few words."""
        if action.kind == "tap":
            x, y, what = await self.point(action)
            await self.adb("shell", f"input tap {x} {y}")
            return f"tapped {what}"
        if action.kind == "type":
            await self.adb("shell", "input text " + typed(action.text))
            if action.submit:
                await self.adb("shell", "input keyevent 66")
            return "typed" + (" and pressed Enter" if action.submit else "")
        if action.kind == "swipe":
            await self.adb(
                "shell", "input swipe " + " ".join(map(str, self.swipe(action))) + " 300"
            )
            return f"swiped {action.direction}"
        if action.kind == "key" and action.key:
            await self.adb("shell", f"input keyevent {KEYCODES[action.key]}")
            return f"pressed {action.key}"
        return ""

    async def point(self, action: DeviceAction) -> tuple[int, int, str]:
        """Where a tap goes: the point given, or the element with that text or name."""
        if action.x is not None and action.y is not None:
            return action.x, action.y, f"at {action.x},{action.y}"
        found = find(await self.ui(), action.target)
        if found is None:
            raise DeviceProblem(f"nothing on the screen is called {action.target!r}",
                                "device_read lists what is there; or tap a point x,y")  # fmt: skip
        x, y = found.center()
        return x, y, f"{found.kind} {action.target!r} at {x},{y}"

    def swipe(self, action: DeviceAction) -> tuple[int, int, int, int]:
        """A swipe across the middle of the screen in the direction asked."""
        width, height = self.size if self.size != (0, 0) else (1080, 1920)
        cx, cy = width // 2, height // 2
        dx, dy = width // 3, height // 3
        return {"up": (cx, cy + dy, cx, cy - dy), "down": (cx, cy - dy, cx, cy + dy),
                "left": (cx + dx, cy, cx - dx, cy),
                "right": (cx - dx, cy, cx + dx, cy)}[action.direction or "up"]  # fmt: skip

    async def ui(self) -> list[Element]:
        """The screen's elements now."""
        await self.adb("shell", f"uiautomator dump {UI_DUMP}", timeout=30)
        dumped = await self.adb("exec-out", f"cat {UI_DUMP}")
        return elements(dumped.out)

    async def screen(self, seq: int, note: str) -> DeviceAnswer:
        """A screenshot as the answer to action `seq`."""
        shot = self.work / "screen.png"
        await self.adb("exec-out", "screencap -p", out=shot)
        data = await asyncio.to_thread(shot.read_bytes)
        self.size = png_size(data)
        return DeviceAnswer(seq=seq, ok=True, text=note, image_b64=encoded(data),
                            media_type="image/png", width=self.size[0],
                            height=self.size[1])  # fmt: skip

    async def close(self) -> None:
        """Stop the emulator and forget the session's files."""
        await self.runner.docker("rm", "-f", self.name, timeout=60)
        await asyncio.to_thread(shutil.rmtree, self.work, True)
