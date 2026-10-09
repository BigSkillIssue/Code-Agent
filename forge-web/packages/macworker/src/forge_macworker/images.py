"""Setting a Mac up: what it needs, and the VM image jobs run in.

The image starts from a macOS image with Xcode (for example Cirrus Labs'
`ghcr.io/cirruslabs/macos-sequoia-xcode`) and gets XcodeGen, uv and Forge's job runner
(`forge-mac-job`). Nothing secret goes into it: jobs bring their project and take results back.
"""

import asyncio
import shutil
import subprocess
from pathlib import Path

from forge_macworker.runners import MOUNT

SETUP_MOUNT = MOUNT.replace("/jobs", "/setup")


def check_setup(image: str, *, direct: bool) -> list[str]:
    """What is missing on this Mac (empty when it is ready)."""
    problems = []
    if direct:
        for tool, hint in (("xcodebuild", "install Xcode from the App Store"),
                           ("xcodegen", "brew install xcodegen")):  # fmt: skip
            if shutil.which(tool) is None:
                problems.append(f"{tool} is missing: {hint}")
        return problems
    if shutil.which("tart") is None:
        return ["tart is missing: brew install cirruslabs/cli/tart"]
    listed = subprocess.run(["tart", "list", "--quiet"], capture_output=True, text=True,
                            check=False)  # fmt: skip
    if image not in listed.stdout.split():
        problems.append(f"the image {image} does not exist: run forge-mac-worker prepare-image")
    if shutil.which("softnet") is None:
        problems.append(
            "softnet is missing (VMs could reach this Mac's network): "
            "brew install cirruslabs/cli/softnet, or run with --network nat"
        )
    return problems


async def prepare_image(base: str, name: str, wheels: Path) -> int:
    """Clone `base` into `name` and install XcodeGen, uv and the job runner in it."""
    found = sorted(p.name for p in wheels.glob("*.whl"))
    if not any(w.startswith("forge_macworker-") for w in found):
        print(f"no forge_macworker wheel in {wheels} (uv build packages/macworker)")
        return 2
    if await tart("clone", base, name) != 0:
        return 1
    vm = await asyncio.create_subprocess_exec(
        "tart", "run", name, "--no-graphics", f"--dir=setup:{wheels.resolve()}",
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
    )  # fmt: skip
    try:
        for _ in range(150):
            if await tart("exec", name, "true", quiet=True) == 0:
                break
            await asyncio.sleep(2)
        install = (
            "export PATH=/opt/homebrew/bin:$HOME/.local/bin:$PATH && "
            "brew install xcodegen uv && "
            f'uv tool install --python 3.12 --find-links "{SETUP_MOUNT}" forge-macworker'
        )
        code = await tart("exec", name, "/bin/zsh", "-lc", install)
    finally:
        await tart("stop", name)
        await vm.wait()
    print("the image is ready" if code == 0 else "installing into the image failed")
    return 0 if code == 0 else 1


async def tart(*args: str, quiet: bool = False) -> int:
    """Run tart and show its output (unless quiet)."""
    out = asyncio.subprocess.DEVNULL if quiet else None
    proc = await asyncio.create_subprocess_exec("tart", *args, stdout=out, stderr=out)
    return await proc.wait()
