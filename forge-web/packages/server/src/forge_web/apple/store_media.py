"""Screenshots for the App Store (W22d): taken on a Mac at the exact sizes Apple takes, checked,
and uploaded into the app's App Asset Library.

The sizes are not hard-coded: Apple's reference data (`appAssetLibraryRefData`) names the
placement groups of each device family and the exact image specifications they accept, and
changes when new devices come. For each device family the largest screenshot size is used,
which is the one Apple asks for first (and shows on smaller devices too).
"""

import asyncio
import base64
import hashlib
import shutil
import struct
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from forge_macworker.wire import PNG, ScreenshotParams, StoreSize
from forge_web.apple.asc_client import AscClient, AscError
from forge_web.apple.jobs import AppleJobs, NewJob
from forge_web.apple.upload import send_part

FAMILIES = {"ios": ("iphone", "ipad", "watch"), "macos": ("mac",)}
SHOOT = {"iphone": "ios", "ipad": "ipados", "watch": "watchos", "mac": "macos"}  # job platforms
SCREENSHOT = "APP_SCREENSHOT"
CATEGORY = "APP_SCREENSHOTS_AND_PREVIEWS"
READY = ("PREPARE_FOR_SUBMISSION", "APPROVED")  # processed and ready to be placed


class MediaProblem(Exception):
    """A store screenshot cannot be made or placed."""


@dataclass(frozen=True)
class Slot:
    """Where a screenshot goes: a device family's placement group and the exact size it takes."""

    family: str
    group: str
    spec: str
    width: int
    height: int

    @property
    def size(self) -> StoreSize:
        return StoreSize(width=self.width, height=self.height)


def family_of(group: dict[str, Any]) -> str:
    """Which device family a placement group is for ("" for those Forge does not make)."""
    words = " ".join(str(group.get(k, "")) for k in ("groupId", "id", "displayClass", "platform"))
    words = words.upper()
    if any(other in words for other in ("IMESSAGE", "VISION", "TV_OS", "APPLE_TV")):
        return ""
    for family, marks in (("ipad", ("IPAD",)), ("watch", ("WATCH",)), ("mac", ("MAC",)),
                          ("iphone", ("IPHONE", "IOS"))):  # fmt: skip
        if any(mark in words for mark in marks):
            return family
    return ""


def slots_from(ref: dict[str, Any], families: tuple[str, ...]) -> list[Slot]:
    """For each family, the screenshot placement group and size with the most pixels."""
    groups = {
        str(g.get("groupId") or g.get("id")): g for g in ref.get("placementProfileGroups", [])
    }
    specs = {str(s.get("specId")): s for s in ref.get("imageSpecs", [])}
    shots: dict[str, Any] = next(
        (t for t in ref.get("placementTypes", []) if type_of(t) == SCREENSHOT), {}
    )
    best: dict[str, Slot] = {}
    for mapping in shots.get("specMappings", []):
        group_id = str(mapping.get("groupId") or mapping.get("placementGroup") or "")
        family = family_of(groups.get(group_id, {"groupId": group_id}))
        for spec_id in mapping.get("specs", []):
            size = exact_size(specs.get(str(spec_id), {}))
            if family in families and size is not None and upright(family, size):
                slot = Slot(family, group_id, str(spec_id), *size)
                if family not in best or area(slot) > area(best[family]):
                    best[family] = slot
    missing = [f for f in families if f not in best]
    if missing:
        raise MediaProblem(
            f"Apple's reference data has no screenshot size for {', '.join(missing)}"
        )
    return [best[f] for f in families]  # fmt: skip


def type_of(entry: dict[str, Any]) -> str:
    """A placement type entry's name (the reference data's key for it)."""
    return str(entry.get("placementType") or entry.get("type") or entry.get("id") or "")


def exact_size(spec: dict[str, Any]) -> tuple[int, int] | None:
    """A specification's exact size (screenshots have no ranges)."""
    d = spec.get("dimensions") or {}
    width, height = d.get("minWidth"), d.get("minHeight")
    if not width or not height or (width, height) != (d.get("maxWidth"), d.get("maxHeight")):
        return None
    return int(width), int(height)


def upright(family: str, size: tuple[int, int]) -> bool:
    """Portrait for iPhone, iPad and Watch (as the app runs there), landscape for the Mac."""
    width, height = size
    return width > height if family == "mac" else height > width


def area(slot: Slot) -> int:
    return slot.width * slot.height


def check_store_png(data: bytes, slot: Slot) -> None:
    """A store screenshot must be a PNG of exactly the slot's size, without transparency."""
    if not data.startswith(PNG) or data[12:16] != b"IHDR":
        raise MediaProblem("the store screenshot is not a PNG")
    width, height = struct.unpack(">II", data[16:24])
    if (width, height) != (slot.width, slot.height):
        raise MediaProblem(f"the {slot.family} screenshot is {width}x{height}, "
                           f"not {slot.width}x{slot.height}")  # fmt: skip
    if data[25] in (4, 6) or "tRNS" in chunk_names(data):
        raise MediaProblem(f"the {slot.family} screenshot has transparency, which Apple refuses")


def chunk_names(data: bytes) -> list[str]:
    """The names of a PNG's chunks up to its image data."""
    names, at = [], len(PNG)
    while at + 8 <= len(data):
        length, name = struct.unpack(">I4s", data[at : at + 8])
        names.append(name.decode("latin-1"))
        if name == b"IDAT":
            break
        at += 12 + length
    return names


async def asset_library(asc: AscClient, app_id: str) -> str:
    """The id of the app's asset library."""
    found = (await asc.get(f"/v1/apps/{app_id}/assetLibrary"))["data"]
    return str(found["id"])


async def upload_image(asc: AscClient, library_id: str, name: str, data: bytes) -> str:
    """Reserve, upload and commit one image; its id."""
    body = {
        "data": {
            "type": "appAssetLibraryImages",
            "attributes": {
                "fileName": name,
                "fileSize": len(data),
                "category": CATEGORY,
                "referenceName": name.rsplit(".", 1)[0],
            },
            "relationships": {
                "assetLibrary": {"data": {"type": "appAssetLibraries", "id": library_id}}
            },
        }
    }
    made = (await asc.post("/v1/appAssetLibraryImages", body))["data"]  # fmt: skip
    for operation in made["attributes"].get("uploadOperations") or []:
        await send_part(asc, operation, data)
    commit = {"data": {"type": "appAssetLibraryImages", "id": made["id"],
                       "attributes": {"uploaded": True}}}  # fmt: skip
    await asc.patch(f"/v1/appAssetLibraryImages/{made['id']}", commit)
    return str(made["id"])


async def processed(asc: AscClient, image_id: str, poll_s: float, tries: int = 120) -> None:
    """Wait until Apple processed an image; MediaProblem with Apple's reason when it failed."""
    for _ in range(tries):
        item = (await asc.get(f"/v1/appAssetLibraryImages/{image_id}"))["data"]["attributes"]
        if item.get("state") in READY:
            return
        if item.get("state") == "FAILED":
            raise MediaProblem(f"Apple could not use a screenshot: {item.get('stateDetails')}")
        await asyncio.sleep(poll_s)
    raise MediaProblem("Apple did not process the screenshots in time")


async def place(asc: AscClient, slot: Slot, image_id: str, localization_id: str) -> str:
    """Show an image as a screenshot of the version's localization; the placement's id."""
    body = {
        "data": {
            "type": "appAssetLibraryPlacements",
            "attributes": {"placementType": SCREENSHOT, "placementGroup": slot.group},
            "relationships": {
                "image": {"data": {"type": "appAssetLibraryImages", "id": image_id}},
                "appStoreVersionLocalization": {
                    "data": {"type": "appStoreVersionLocalizations", "id": localization_id}
                },
            },
        }
    }
    try:
        made = await asc.post("/v1/appAssetLibraryPlacements", body)  # fmt: skip
    except AscError as err:
        raise MediaProblem(f"the {slot.family} screenshot could not be placed: {err}") from None
    return str(made["data"]["id"])


def shot_name(slot: Slot, dark: bool, data: bytes) -> str:
    """A file name that says what the picture is (and differs when the picture does)."""
    mode = "dark" if dark else "light"
    return f"forge-{slot.family}-{mode}-{hashlib.sha256(data).hexdigest()[:8]}.png"


@dataclass(frozen=True)
class Shot:
    """A store screenshot: where it goes, light or dark, and the picture."""

    slot: Slot
    dark: bool
    data: bytes


def families_for(platform: str, bundles: list[str]) -> tuple[str, ...]:
    """The device families a release shows: the Watch only when the app has a Watch app."""
    families = FAMILIES[platform]
    has_watch = any(b.endswith((".watchkitapp", ".watchapp")) for b in bundles)
    return tuple(f for f in families if f != "watch" or has_watch)


async def take_shots(
    jobs: AppleJobs, owner: NewJob, source: Path, slots: list[Slot], work: Path
) -> list[Shot]:
    """Light and dark pictures of every slot's device, from the packed commit in `source`
    (`owner` says whose project and job they are; its params are not used)."""
    shots = []
    work.mkdir(parents=True, exist_ok=True)
    for slot in slots:
        for dark in (False, True):
            copy = work / f"{slot.family}-{int(dark)}.tar.gz"  # a job takes its source away
            await asyncio.to_thread(shutil.copyfile, source, copy)
            params = ScreenshotParams(platform=SHOOT[slot.family], dark=dark, fit=slot.size)
            job = NewJob(owner.project_id, owner.chat_id, owner.user_id, params)
            result = await jobs.run(job, copy)
            if not result.ok or result.screen is None:
                raise MediaProblem(f"the {slot.family} screenshot could not be taken: "
                                   f"{result.error or 'no picture came back'}")  # fmt: skip
            data = base64.b64decode(result.screen.image.data_b64)
            check_store_png(data, slot)
            shots.append(Shot(slot, dark, data))
    return shots


async def upload_shots(
    asc: AscClient, app_id: str, shots: list[Shot], poll_s: float
) -> list[dict[str, Any]]:
    """Every shot in the app's asset library, processed by Apple; what was uploaded."""
    library = await asset_library(asc, app_id)
    uploaded: list[dict[str, Any]] = []
    for shot in shots:
        image_id = await upload_image(asc, library, shot_name(shot.slot, shot.dark, shot.data),
                                      shot.data)  # fmt: skip
        uploaded.append(
            {
                "family": shot.slot.family,
                "group": shot.slot.group,
                "spec": shot.slot.spec,
                "dark": shot.dark,
                "image_id": image_id,
            }
        )
    for item in uploaded:
        await processed(asc, item["image_id"], poll_s)
    return uploaded
