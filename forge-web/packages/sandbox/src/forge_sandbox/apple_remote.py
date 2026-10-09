"""Apple builds from a sandbox: Forge's AppleBuilder port, carried out on a Mac of the server.

Each call packs the project (without git data, Forge's files and build output) and sends it
through the gateway with the chat's run token; the server queues it for a Mac, which builds the
project in a VM of its own and sends back the result.
"""

import asyncio
import os
import tarfile
import tempfile
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
from forge.ports import (
    AppleAction,
    AppleBuildError,
    AppleBuildResult,
    ApplePlatform,
    AppleScreen,
)

# Folders that never go to the Mac: history, Forge's own files, build output, dependencies.
SKIPPED = frozenset({".git", ".forge", "DerivedData", "build", ".build", "node_modules",
                     "xcuserdata", ".swiftpm", "Pods"})  # fmt: skip


class RemoteAppleBuilder:
    """Builds, tests and photographs the project at `root` on the server's Macs."""

    def __init__(
        self,
        root: Path,
        base_url: str,
        token_env: str,
        *,
        max_mb: int = 300,
        timeout_s: float = 2_700,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.root = root
        self.token_env = token_env
        self.max_bytes = max_mb * 1024 * 1024
        self.client = httpx.AsyncClient(base_url=base_url.rstrip("/"), transport=transport,
                                        timeout=httpx.Timeout(timeout_s, connect=30))  # fmt: skip

    async def build(
        self, platform: ApplePlatform, action: AppleAction, scheme: str | None = None
    ) -> AppleBuildResult:
        """Build, test or archive for one platform."""
        params = {"platform": platform, "action": action, **({"scheme": scheme} if scheme else {})}
        return AppleBuildResult.model_validate(await self.call("/apple/build", params))

    async def screenshot(
        self, platform: ApplePlatform, device: str | None = None, dark: bool = False
    ) -> AppleScreen:
        """A picture of the app on a simulated device (or the Mac)."""
        params = {"platform": platform, "dark": "true" if dark else "false",
                  **({"device": device} if device else {})}  # fmt: skip
        return AppleScreen.model_validate(await self.call("/apple/screenshot", params))

    async def close(self) -> None:
        """Close the connection pool."""
        await self.client.aclose()

    async def call(self, path: str, params: dict[str, str]) -> dict[str, Any]:
        """Send the packed project with these parameters; the answer, or AppleBuildError."""
        packed = await asyncio.to_thread(pack, self.root, self.max_bytes)
        headers = {"Authorization": f"Bearer {os.environ.get(self.token_env, '')}",
                   "Content-Type": "application/gzip"}  # fmt: skip
        try:
            response = await self.client.post(path, params=params, content=chunks(packed),
                                              headers=headers)  # fmt: skip
        except httpx.HTTPError as err:
            raise AppleBuildError(f"the Mac build service could not be reached: {err}") from None
        finally:
            packed.unlink(missing_ok=True)
        try:
            data = response.json()
        except ValueError:
            data = {}
        if response.status_code != 200 or not isinstance(data, dict):
            error = data.get("error") if isinstance(data, dict) else None
            hint = data.get("hint", "") if isinstance(data, dict) else ""
            message = error or f"the Mac build service answered {response.status_code}"
            raise AppleBuildError(str(message), hint=str(hint))
        return data


async def chunks(path: Path) -> AsyncIterator[bytes]:
    """A file in pieces, read off the event loop."""
    with path.open("rb") as file:
        while chunk := await asyncio.to_thread(file.read, 1024 * 1024):
            yield chunk


def pack(root: Path, max_bytes: int) -> Path:
    """The project as a tar.gz in a temporary file (AppleBuildError when it is too large)."""
    fd, name = tempfile.mkstemp(suffix=".tar.gz", prefix="forge-apple-")
    os.close(fd)
    target, total = Path(name), 0
    try:
        with tarfile.open(target, "w:gz") as archive:
            for path in project_files(root):
                total += path.lstat().st_size
                if total > max_bytes:
                    raise AppleBuildError(
                        f"the project is larger than {max_bytes // (1024 * 1024)} MB",
                        hint="keep build output, videos and other large files out of the project",
                    )
                archive.add(path, arcname=path.relative_to(root).as_posix(), recursive=False)
    except BaseException:
        target.unlink(missing_ok=True)
        raise
    return target


def project_files(root: Path) -> list[Path]:
    """Every file, folder and link of the project except the skipped folders."""
    found: list[Path] = []
    for folder, dirs, files in os.walk(root):
        dirs[:] = sorted(d for d in dirs if d not in SKIPPED)
        base = Path(folder)
        found += [base / d for d in dirs if (base / d).is_symlink()]  # links are kept as links
        dirs[:] = [d for d in dirs if not (base / d).is_symlink()]
        found += [base / f for f in sorted(files)]
    return found
