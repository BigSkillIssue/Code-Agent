"""Store screenshots (W22d1): Apple's sizes read from its reference data, pictures fitted to them
on the Mac (scaled, padded, no transparency), checked on the server and uploaded into the
app's asset library of the stand-in for App Store Connect."""

import asyncio
import base64
import shutil
import struct
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from forge.ports import AppleScreen
from forge.providers.base import ImagePart
from sqlalchemy import select

from asc_standin import ISSUER_ID, KEY_ID, TEAM_ID, AscStandIn
from asc_standin_store import png, ref_data
from forge_macworker.client import WorkerClient
from forge_macworker.runners import DirectRunner
from forge_macworker.store_shots import fit_screenshot, scaled
from forge_macworker.wire import StoreSize, knows_store_shots
from forge_web.apple.asc_client import AscClient, AscKey
from forge_web.apple.jobs import NewJob, can_run
from forge_web.apple.store_media import (
    MediaProblem,
    Slot,
    check_store_png,
    families_for,
    slots_from,
    take_shots,
    upload_shots,
)
from forge_web.db.models import AppleJob
from test_apple_server import world  # noqa: F401  (the server's Apple world)
from test_macworker import Builder, packed

REF = ref_data()["data"][0]["attributes"]


def screen(data: bytes, platform: str = "ios") -> AppleScreen:
    return AppleScreen(platform=platform, device="iPhone 16", dark=False,  # type: ignore[arg-type]
                       image=ImagePart(media_type="image/png",
                                       data_b64=base64.b64encode(data).decode()))  # fmt: skip


async def fake_sips(argv: list[str], cwd: Path) -> tuple[int, str]:
    """Stands in for macOS's sips: makes white pictures of the sizes asked for."""
    out = cwd / argv[argv.index("--out") + 1]
    source = cwd / argv[-3]
    if argv[1] == "-z" or argv[1] == "-p":  # scale to height, width
        out.write_bytes(png(int(argv[3]), int(argv[2]), alpha=True))
    elif argv[1:4] == ["-s", "format", "jpeg"]:
        out.write_bytes(b"jpeg of " + source.read_bytes()[16:24])  # keeps the size
    else:  # back to PNG: no alpha after JPEG
        width, height = struct.unpack(">II", source.read_bytes()[8:16])
        out.write_bytes(png(width, height))
    return 0, ""


def test_apples_sizes_come_from_its_reference_data() -> None:
    slots = {
        s.family: (s.group, s.width, s.height)
        for s in slots_from(REF, ("iphone", "ipad", "mac", "watch"))
    }
    assert slots == {"iphone": ("IPHONE_DYNAMIC_ISLAND_LARGE_PROFILE", 1320, 2868),  # the largest
                     "ipad": ("IPAD_PRO_13_PROFILE", 2064, 2752),
                     "mac": ("MAC_PROFILE", 2880, 1800),
                     "watch": ("WATCH_ULTRA_PROFILE", 422, 514)}  # fmt: skip
    no_watch = {**REF, "placementProfileGroups": [g for g in REF["placementProfileGroups"]
                                                  if "WATCH" not in g["groupId"]]}  # fmt: skip
    no_watch["placementTypes"] = [
        {
            **REF["placementTypes"][0],
            "specMappings": [
                m for m in REF["placementTypes"][0]["specMappings"] if "WATCH" not in m["groupId"]
            ],
        }
    ]
    with pytest.raises(MediaProblem, match="watch"):
        slots_from(no_watch, ("iphone", "watch"))
    assert families_for("ios", ["com.example.tally", "com.example.tally.watchkitapp"]) == (
        "iphone",
        "ipad",
        "watch",
    )
    assert families_for("ios", ["com.example.tally"]) == ("iphone", "ipad")
    assert families_for("macos", ["com.example.tally"]) == ("mac",)


def test_a_store_screenshot_is_exactly_its_size_without_transparency() -> None:
    slot = Slot("iphone", "G", "S", 1320, 2868)
    check_store_png(png(1320, 2868), slot)
    for bad, reason in ((png(1290, 2796), "1290x2796"),
                        (png(1320, 2868, alpha=True), "transparency"),
                        (b"\xff\xd8 not a png", "not a PNG")):  # fmt: skip
        with pytest.raises(MediaProblem, match=reason):
            check_store_png(bad, slot)


async def test_the_mac_fits_a_picture_to_the_store_size() -> None:
    assert scaled((1179, 2556), StoreSize(width=1320, height=2868)) == (1320, 2862)  # then padded
    assert scaled((3000, 2000), StoreSize(width=2880, height=1800)) == (2700, 1800)
    calls: list[list[str]] = []

    async def recording(argv: list[str], cwd: Path) -> tuple[int, str]:
        calls.append(argv)
        return await fake_sips(argv, cwd)

    fitted = await fit_screenshot(
        screen(png(1179, 2556, alpha=True)), StoreSize(width=1320, height=2868), recording
    )
    data = base64.b64decode(fitted.image.data_b64)
    check_store_png(data, Slot("iphone", "G", "S", 1320, 2868))
    assert [c[1] for c in calls] == ["-z", "-p", "-s", "-s"]  # scale, pad, flatten, back to PNG

    async def broken(argv: list[str], cwd: Path) -> tuple[int, str]:
        return 1, "Error: unable to render"

    with pytest.raises(RuntimeError, match="unable to render"):
        await fit_screenshot(screen(png(10, 20)), StoreSize(width=100, height=200), broken)


async def fitter(shot: AppleScreen, box: StoreSize) -> AppleScreen:
    return await fit_screenshot(shot, box, fake_sips)


class Shooter(Builder):
    """A stand-in Xcode whose pictures are smaller than the store's, with transparency."""

    async def screenshot(
        self, platform: Any, device: str | None = None, dark: bool = False
    ) -> AppleScreen:
        return screen(png(300, 600, alpha=True), platform)


@pytest.fixture
def asc() -> Iterator[AscStandIn]:
    with AscStandIn() as standin:
        standin.state.release.part_size = 4096
        yield standin


async def test_store_screenshots_from_the_mac_into_the_asset_library(
    world: Any,  # noqa: F811
    asc: AscStandIn,
    tmp_path: Path,
) -> None:
    source = tmp_path / "commit.tar.gz"
    source.write_bytes(packed({"Shared/App.swift": b"@main"}))
    slots = slots_from(REF, ("iphone", "watch"))
    owner = NewJob("p1", "c1", "u1", None)  # type: ignore[arg-type]
    taking = asyncio.create_task(take_shots(world.jobs, owner, source, slots, tmp_path / "shots"))
    await asyncio.sleep(0.1)
    async with world.db.session() as session:
        [queued] = await session.scalars(select(AppleJob).where(AppleJob.status == "queued"))
    assert not can_run(queued, "0.2.0") and can_run(queued, "0.3.0")  # older workers cannot fit
    transport = httpx.ASGITransport(app=world.mac._transport.app)
    runner = DirectRunner(tmp_path / "mac", Shooter, fitter=fitter)
    worker = WorkerClient("http://srv", world.token, runner, slots=1, transport=transport)
    stop = asyncio.Event()
    serving = asyncio.create_task(worker.serve(stop))
    shots = await asyncio.wait_for(taking, 60)
    stop.set()
    await asyncio.wait_for(serving, 10)
    taken = [(s.slot.family, s.dark) for s in shots]
    assert taken == [("iphone", False), ("iphone", True), ("watch", False), ("watch", True)]
    app_id = asc.add_app("Tally", "com.example.tally")
    client = AscClient(asc.url, AscKey(KEY_ID, ISSUER_ID, TEAM_ID, asc.pem))
    uploaded = await upload_shots(client, app_id, shots, poll_s=0.01)
    await client.close()
    images = asc.state.store.images
    assert [images[u["image_id"]]["attributes"]["state"] for u in uploaded] == [
        "PREPARE_FOR_SUBMISSION"
    ] * 4
    assert images[uploaded[0]["image_id"]]["attributes"]["specId"] == "spec-1320x2868"
    assert images[uploaded[0]["image_id"]]["data"] == shots[0].data  # whole, in parts
    assert asc.state.release.credentials_at_upload == 0
    assert (
        knows_store_shots("0.3.0") and not knows_store_shots("0.2.9") and not knows_store_shots("x")
    )


@pytest.mark.mac
@pytest.mark.skipif(sys.platform != "darwin" or shutil.which("sips") is None,
                    reason="needs macOS's sips")  # fmt: skip
async def test_the_real_sips_makes_a_store_screenshot() -> None:
    for size, box in (((1179, 2556), (1320, 2868)), ((1600, 1000), (2880, 1800))):
        fitted = await fit_screenshot(screen(png(*size, alpha=True)), StoreSize(width=box[0],
                                      height=box[1]))  # fmt: skip
        check_store_png(base64.b64decode(fitted.image.data_b64), Slot("x", "G", "S", *box))
