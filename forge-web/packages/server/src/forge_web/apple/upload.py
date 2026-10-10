"""Uploading a build to App Store Connect (W22b): the Build Upload API, Apple's processing, and
an internal TestFlight group that gets every build.

The server uploads itself, so the API key never leaves it. Upload URLs come from Apple and are
already signed: they get no token, and only addresses of Apple (or, in tests, the stand-in) are
used.
"""

import asyncio
import hashlib
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx

from forge_web.apple.asc_client import AscClient, AscError

PLATFORMS = {"ios": "IOS", "macos": "MAC_OS"}
UTIS = {"ios": "com.apple.ipa", "macos": "com.apple.pkg"}
GROUP = "Forge"  # the internal TestFlight group Forge makes
PART_TRIES = 3


def upload_url_allowed(url: str, base_url: str) -> bool:
    """Apple's own hosts over https; the stand-in on this machine when the API is it."""
    parts, base = urlsplit(url), urlsplit(base_url)
    host = parts.hostname or ""
    if parts.scheme == "https" and (host == "apple.com" or host.endswith(".apple.com")):
        return True
    local = base.scheme == "http" and base.hostname == "127.0.0.1"
    return local and (parts.scheme, parts.netloc) == (base.scheme, base.netloc)


async def start_upload(asc: AscClient, app_id: str, platform: str, version: str, build: str) -> str:
    """Tell Apple a build is coming; the upload's id."""
    body = {
        "data": {
            "type": "buildUploads",
            "attributes": {
                "cfBundleShortVersionString": version,
                "cfBundleVersion": build,
                "platform": PLATFORMS[platform],
            },
            "relationships": {"app": {"data": {"type": "apps", "id": app_id}}},
        }
    }
    made = await asc.post("/v1/buildUploads", body)  # fmt: skip
    return str(made["data"]["id"])


async def send_file(asc: AscClient, upload_id: str, platform: str, product: Path) -> None:
    """Send the .ipa or .pkg in the parts Apple asks for, then say it is complete."""
    body = {
        "data": {
            "type": "buildUploadFiles",
            "attributes": {
                "assetType": "ASSET",
                "fileName": product.name,
                "fileSize": product.stat().st_size,
                "uti": UTIS[platform],
            },
            "relationships": {"buildUpload": {"data": {"type": "buildUploads", "id": upload_id}}},
        }
    }
    made = (await asc.post("/v1/buildUploadFiles", body))["data"]  # fmt: skip
    for operation in made["attributes"].get("uploadOperations") or []:
        await send_part(asc, operation, product)
    checksum = await asyncio.to_thread(md5_of, product)
    done = {
        "data": {
            "type": "buildUploadFiles",
            "id": made["id"],
            "attributes": {
                "uploaded": True,
                "sourceFileChecksums": {"file": {"hash": checksum, "algorithm": "MD5"}},
            },
        }
    }
    await asc.patch(f"/v1/buildUploadFiles/{made['id']}", done)  # fmt: skip


async def send_part(asc: AscClient, operation: dict[str, Any], product: Path) -> None:
    """One part, to the URL Apple gave (without the API token)."""
    url = str(operation.get("url", ""))
    if not upload_url_allowed(url, asc.base_url):
        raise AscError(
            0, f"Apple asked for an upload to an unexpected address ({urlsplit(url).hostname})"
        )
    offset, length = int(operation.get("offset", 0)), int(operation.get("length", 0))
    data = await asyncio.to_thread(read_range, product, offset, length)
    headers = {str(h["name"]): str(h["value"]) for h in operation.get("requestHeaders") or []}
    for attempt in range(PART_TRIES):
        try:
            reply = await asc.http.request(str(operation.get("method", "PUT")), url,
                                           content=data, headers=headers)  # fmt: skip
        except httpx.TransportError as err:
            if attempt == PART_TRIES - 1:
                raise AscError(0, f"an upload part could not be sent: {err}") from None
            continue
        if reply.status_code < 400:
            return
        if reply.status_code < 500 or attempt == PART_TRIES - 1:
            raise AscError(reply.status_code, f"Apple refused an upload part ({reply.status_code})")


def read_range(path: Path, offset: int, length: int) -> bytes:
    with path.open("rb") as source:
        source.seek(offset)
        return source.read(length)


def md5_of(path: Path) -> str:
    digest = hashlib.md5(usedforsecurity=False)  # what Apple asks for
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


async def upload_state(asc: AscClient, upload_id: str) -> tuple[str, str, str]:
    """Where Apple is with an upload: its state, the build's id once there is one, and Apple's
    reasons when it failed."""
    item = (await asc.get(f"/v1/buildUploads/{upload_id}"))["data"]
    state = item.get("attributes", {}).get("state")
    code = str(state.get("state", "") if isinstance(state, dict) else state or "")
    errors = state.get("errors", []) if isinstance(state, dict) else []
    reasons = "; ".join(str(e.get("description") or e.get("detail") or e.get("code") or e)
                        for e in errors if e)  # fmt: skip
    build = (item.get("relationships", {}).get("build") or {}).get("data") or {}
    return code, str(build.get("id", "")), reasons


async def build_state(asc: AscClient, build_id: str) -> str:
    """Apple's processing of a build: PROCESSING, VALID, INVALID or FAILED."""
    item = (await asc.get(f"/v1/builds/{build_id}"))["data"]
    return str(item.get("attributes", {}).get("processingState", ""))


async def testers_group(asc: AscClient, app_id: str) -> str:
    """The app's internal TestFlight group "Forge" with access to every build (made once)."""
    if found := await forge_group(asc, app_id):
        return found
    body = {
        "data": {
            "type": "betaGroups",
            "attributes": {"name": GROUP, "isInternalGroup": True, "hasAccessToAllBuilds": True},
            "relationships": {"app": {"data": {"type": "apps", "id": app_id}}},
        }
    }
    try:
        made = await asc.post("/v1/betaGroups", body)
    except AscError as err:  # the other platform's release made it a moment ago
        if err.status == 409 and (found := await forge_group(asc, app_id)):
            return found
        raise
    return str(made["data"]["id"])


async def forge_group(asc: AscClient, app_id: str) -> str:
    """The id of the app's group "Forge" ("" when there is none)."""
    for group in await asc.all(f"/v1/apps/{app_id}/betaGroups"):
        if group.get("attributes", {}).get("name") == GROUP:
            return str(group["id"])
    return ""
