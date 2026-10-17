"""The Windows apps' downloads on their websites (W39).

Forge Web says what each app on this host offers (`GET /api/host/downloads`, its newest
published release); the worker fetches the files with its host token, checks each one's size
and SHA-256, and keeps them where Caddy serves them: `https://<app>/downloads/windows/<file>`.
So a product's program is never served from Forge Web's own domain, and the downloads cost
the host's bandwidth, not Forge Web's. A folder is replaced only once all its new files are
there and checked; an app that offers nothing any more loses its folder.
"""

import asyncio
import hashlib
import json
import logging
import shutil
from pathlib import Path

import httpx

from forge_hostworker.wire import AppDownloads, DownloadsList

DOWNLOADS = Path("/var/lib/caddy/forge-downloads")  # Caddy reads it; the worker writes it
MANIFEST = ".files.json"  # name -> SHA-256 of the files a folder holds, checked
log = logging.getLogger(__name__)


class DownloadProblem(Exception):
    """A file did not arrive as Forge Web described it."""


class Downloads:
    """Keeps every app's download folder in step with Forge Web."""

    def __init__(self, http: httpx.AsyncClient, root: Path = DOWNLOADS) -> None:
        self.http = http
        self.root = root

    def folder(self, app: str) -> Path:
        return self.root / app / "windows"

    def offered(self, app: str) -> Path | None:
        """The folder Caddy serves for this app, when it holds a checked release."""
        folder = self.folder(app)
        return folder if (folder / MANIFEST).is_file() else None

    async def sync(self) -> None:
        """Fetch what changed and remove what is no longer offered."""
        reply = await self.http.get("/api/host/downloads")
        reply.raise_for_status()
        listed = DownloadsList.model_validate_json(reply.content)
        await asyncio.to_thread(self.root.mkdir, mode=0o755, parents=True, exist_ok=True)
        for entry in listed.apps:
            try:
                await self.keep(entry)
            except (DownloadProblem, httpx.HTTPError, OSError) as err:
                log.warning("the downloads of %s stay as they were: %s", entry.app, err)
        wanted = {entry.app for entry in listed.apps if entry.files}
        for folder in sorted(self.root.iterdir()):
            if folder.is_dir() and folder.name not in wanted:
                await asyncio.to_thread(shutil.rmtree, folder, True)

    async def keep(self, entry: AppDownloads) -> None:
        """One app's folder with exactly the files Forge Web lists."""
        folder = self.folder(entry.app)
        want = {f.name: f.sha256 for f in entry.files}
        if not want or self.held(folder) == want:
            return
        fresh = folder.with_name(".windows.new")
        await asyncio.to_thread(shutil.rmtree, fresh, True)
        fresh.mkdir(mode=0o755, parents=True)
        try:
            for file in entry.files:
                await self.fetch(entry.app, file.name, file.size, file.sha256, fresh / file.name)
        except BaseException:
            await asyncio.to_thread(shutil.rmtree, fresh, True)
            raise
        (fresh / MANIFEST).write_text(json.dumps(want), encoding="utf-8")
        old = folder.with_name(".windows.old")
        await asyncio.to_thread(shutil.rmtree, old, True)
        if folder.exists():
            folder.rename(old)
        fresh.rename(folder)
        await asyncio.to_thread(shutil.rmtree, old, True)
        log.info("the downloads of %s are now %s", entry.app, ", ".join(sorted(want)))

    def held(self, folder: Path) -> dict[str, str]:
        """The checked files a folder holds, by name."""
        try:
            found = json.loads((folder / MANIFEST).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return found if isinstance(found, dict) else {}

    async def fetch(self, app: str, name: str, size: int, sha256: str, target: Path) -> None:
        """One file, checked against its size and SHA-256 as it arrives."""
        digest, received = hashlib.sha256(), 0
        async with self.http.stream("GET", f"/api/host/downloads/{app}/{name}",
                                    timeout=httpx.Timeout(600, connect=30)) as reply:  # fmt: skip
            reply.raise_for_status()
            with target.open("wb") as out:
                async for part in reply.aiter_bytes():
                    received += len(part)
                    if received > size:
                        raise DownloadProblem(f"{name} is larger than Forge Web said")
                    digest.update(part)
                    await asyncio.to_thread(out.write, part)
        if received != size or digest.hexdigest().upper() != sha256:
            raise DownloadProblem(f"{name} is not the file Forge Web described")
        target.chmod(0o644)
