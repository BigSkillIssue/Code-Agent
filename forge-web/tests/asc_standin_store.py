"""The App Store half of the App Store Connect stand-in (W22d): the App Asset Library with its
reference data, images and placements.

Apple's rules that matter here are kept: an image must match one of the exact specifications
and have no transparency, a placement must use a group that takes the image's specification,
and a group holds at most ten screenshots per localization.
"""

import struct
import zlib
from dataclasses import dataclass, field
from typing import Any

from fastapi import FastAPI, Request

from asc_standin_release import ReleaseState, new_id, resource

PNG = b"\x89PNG\r\n\x1a\n"
GROUPS = [
    ("IPHONE_DYNAMIC_ISLAND_LARGE_PROFILE", "IOS", "IPHONE_69"),
    ("IPHONE_HOME_BUTTON_PROFILE", "IOS", "IPHONE_55"),
    ("IPAD_PRO_13_PROFILE", "IOS", "IPAD_PRO_129"),
    ("MAC_PROFILE", "MAC_OS", "DESKTOP"),
    ("WATCH_ULTRA_PROFILE", "WATCH_OS", "APPLE_WATCH_ULTRA"),
    ("IMESSAGE_IPHONE_DYNAMIC_ISLAND_LARGE_PROFILE", "IOS", "IPHONE_69"),
]
SIZES = {
    "IPHONE_DYNAMIC_ISLAND_LARGE_PROFILE": [(1290, 2796), (1320, 2868), (2868, 1320)],
    "IPHONE_HOME_BUTTON_PROFILE": [(1242, 2208)],
    "IPAD_PRO_13_PROFILE": [(2048, 2732), (2064, 2752)],
    "MAC_PROFILE": [(1280, 800), (1440, 900), (2560, 1600), (2880, 1800)],
    "WATCH_ULTRA_PROFILE": [(410, 502), (422, 514)],
}
MAX_PER_GROUP = 10


def png(width: int, height: int, alpha: bool = False) -> bytes:
    """A plain PNG of this size (white; with an alpha channel when asked)."""
    pixel = b"\xff\xff\xff\xff" if alpha else b"\xff\xff\xff"
    rows = b"".join(b"\0" + pixel * width for _ in range(height))

    def chunk(name: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data)) + name + data + struct.pack(">I", zlib.crc32(name + data))
        )

    header = struct.pack(">IIBBBBB", width, height, 8, 6 if alpha else 2, 0, 0, 0)
    return PNG + chunk(b"IHDR", header) + chunk(b"IDAT", zlib.compress(rows)) + chunk(b"IEND", b"")


def spec_id(width: int, height: int) -> str:
    return f"spec-{width}x{height}"


def ref_data() -> dict[str, Any]:
    """The reference data, shaped like Apple's (one item with parallel arrays)."""
    sizes = {size for group in SIZES.values() for size in group}
    specs = [{"specId": spec_id(w, h), "shortName": f"i{w}x{h}a0", "alphaAllowed": False,
              "dimensions": {"minWidth": w, "maxWidth": w, "minHeight": h, "maxHeight": h},
              "compatiblePlacementTypes": ["APP_SCREENSHOT"], "fileExtensions": [".png", ".jpg"]}
             for w, h in sorted(sizes)]  # fmt: skip
    mappings = [{"groupId": g, "specs": [spec_id(w, h) for w, h in sizes_]}
                for g, sizes_ in SIZES.items()]  # fmt: skip
    return {"data": [{"type": "appAssetLibraryRefData", "id": "ref", "attributes": {
        "placementProfileGroups": [{"groupId": g, "platform": p, "displayClass": d}
                                   for g, p, d in GROUPS],
        "imageSpecs": specs,
        "placementTypes": [{"placementType": "APP_SCREENSHOT",
                            "acceptsAssetCategories": ["APP_SCREENSHOTS_AND_PREVIEWS"],
                            "specMappings": mappings}],
    }}], "links": {}}  # fmt: skip


@dataclass
class StoreState:
    """What the asset library knows."""

    images: dict[str, dict[str, Any]] = field(default_factory=dict)
    placements: dict[str, dict[str, Any]] = field(default_factory=dict)
    localizations: dict[str, dict[str, Any]] = field(default_factory=dict)  # W22d2 fills them


def image_problem(data: bytes) -> tuple[str, str]:
    """The specification an image matches, or why it matches none."""
    if not data.startswith(PNG):
        return "", "The file is not an image Apple takes."
    width, height = struct.unpack(">II", data[16:24])
    if data[25] in (4, 6):
        return "", "Images can't contain alpha channels or transparencies."
    if not any((width, height) in sizes for sizes in SIZES.values()):
        return "", f"The dimensions {width}x{height} match no screenshot specification."
    return spec_id(width, height), ""


def library_routes(
    app: FastAPI, state: StoreState, release: ReleaseState, apps: Any, error: Any
) -> None:
    """Reference data, the library and its images."""

    @app.get("/v1/appAssetLibraryRefData")
    async def refs() -> Any:
        return ref_data()

    @app.get("/v1/apps/{app_id}/assetLibrary")
    async def library(app_id: str) -> Any:
        if not any(a["id"] == app_id for a in apps.apps):
            return error(404, "There is no resource of type 'apps' with id " + app_id)
        return resource("appAssetLibraries", f"lib-{app_id}", {})

    @app.post("/v1/appAssetLibraryImages")
    async def new_image(request: Request) -> Any:
        data = (await request.json())["data"]
        attributes = data["attributes"]
        if attributes.get("category") != "APP_SCREENSHOTS_AND_PREVIEWS":
            return error(409, "category must be APP_SCREENSHOTS_AND_PREVIEWS for screenshots")
        iid, size = new_id(), int(attributes["fileSize"])
        operations = [{"method": "PUT", "url": f"{release.base_url}/upload/{iid}/{n}",
                       "offset": offset, "length": min(release.part_size, size - offset),
                       "requestHeaders": [{"name": "Content-Type", "value": "image/png"}]}
                      for n, offset in enumerate(range(0, size, release.part_size))]  # fmt: skip
        release.files[iid] = {"upload": "", "size": size, "parts": {}, "operations": operations}
        state.images[iid] = {
            "seen": 0,
            "data": b"",
            "attributes": {
                **attributes,
                "state": "AWAITING_UPLOAD",
                "specId": None,
                "stateDetails": None,
            },
        }
        shown = {**state.images[iid]["attributes"], "uploadOperations": operations}
        return resource("appAssetLibraryImages", iid, shown)

    @app.patch("/v1/appAssetLibraryImages/{iid}")
    async def committed(iid: str, request: Request) -> Any:
        attributes = (await request.json())["data"]["attributes"]
        image, parts = state.images.get(iid), release.files.get(iid)
        if image is None or parts is None or not attributes.get("uploaded"):
            return error(409, "nothing was uploaded")
        whole = b"".join(parts["parts"][n] for n in sorted(parts["parts"]))
        if len(whole) != parts["size"] or "sourceFileChecksum" in attributes:
            return error(409, "The upload does not match its size (and takes no checksum).")
        spec, problem = image_problem(whole)
        image["data"] = whole
        image["attributes"].update(state="FAILED" if problem else "UPLOAD_COMPLETE",
                                   stateDetails=problem or None, specId=spec or None)  # fmt: skip
        return resource("appAssetLibraryImages", iid, image["attributes"])

    @app.get("/v1/appAssetLibraryImages/{iid}")
    async def image_state(iid: str) -> Any:
        image = state.images.get(iid)
        if image is None:
            return error(404, "There is no such image.")
        image["seen"] += 1
        if image["attributes"]["state"] == "UPLOAD_COMPLETE" and image["seen"] >= release.polls:
            image["attributes"]["state"] = "PREPARE_FOR_SUBMISSION"
        return resource("appAssetLibraryImages", iid, image["attributes"])

    @app.post("/v1/appAssetLibraryPlacements")
    async def new_placement(request: Request) -> Any:
        data = (await request.json())["data"]
        group = data["attributes"]["placementGroup"]
        image = state.images.get(data["relationships"]["image"]["data"]["id"])
        localization = data["relationships"]["appStoreVersionLocalization"]["data"]["id"]
        if localization not in state.localizations:
            return error(404, "There is no such App Store version localization.")
        spec = (image or {}).get("attributes", {}).get("specId")
        if image is None or spec not in [spec_id(w, h) for w, h in SIZES.get(group, [])]:
            return error(409, "The requested 'placementType' and 'placementGroup' combination "
                              "is not supported for this parent.")  # fmt: skip
        used = [p for p in state.placements.values()
                if p["localization"] == localization and p["group"] == group]  # fmt: skip
        if len(used) >= MAX_PER_GROUP:
            return error(409, f"A placement group holds at most {MAX_PER_GROUP} screenshots.")
        pid = new_id()
        state.placements[pid] = {"localization": localization, "group": group,
                                 "image": data["relationships"]["image"]["data"]["id"]}  # fmt: skip
        return resource("appAssetLibraryPlacements", pid,
                        {"placementType": "APP_SCREENSHOT", "placementGroup": group,
                         "state": "PARENT_PREPARE_FOR_SUBMISSION"})  # fmt: skip
