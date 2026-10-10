"""Screenshots for the App Store (W22d): the picture of a device made exactly the size Apple
takes for it, with macOS's own `sips`.

The picture is scaled to fit, padded to the exact size (Apple takes no other) and flattened
through JPEG, so it has no transparency left, which Apple does not accept either.
"""

import base64
import struct
import tempfile
from pathlib import Path

from forge.ports import AppleScreen
from forge.providers.base import ImagePart

from forge_macworker.export import Run, run_command
from forge_macworker.wire import PNG, StoreSize

PAD = "FFFFFF"


def png_size(data: bytes) -> tuple[int, int]:
    """Width and height from a PNG's header."""
    if not data.startswith(PNG) or data[12:16] != b"IHDR":
        raise RuntimeError("the screenshot is not a PNG")
    width, height = struct.unpack(">II", data[16:24])
    return int(width), int(height)


def scaled(size: tuple[int, int], box: StoreSize) -> tuple[int, int]:
    """The largest size with the picture's proportions that fits in the box."""
    width, height = size
    factor = min(box.width / width, box.height / height)
    return max(1, min(box.width, round(width * factor))), max(
        1, min(box.height, round(height * factor))
    )


async def fit_screenshot(
    screen: AppleScreen, box: StoreSize, run: Run = run_command
) -> AppleScreen:
    """The screenshot at exactly the box's size, as a PNG without an alpha channel."""
    data = base64.b64decode(screen.image.data_b64)
    width, height = scaled(png_size(data), box)
    with tempfile.TemporaryDirectory(prefix="forge-store-") as folder:
        work = Path(folder)
        (work / "shot.png").write_bytes(data)
        for argv in (
            ["sips", "-z", str(height), str(width), "shot.png", "--out", "scaled.png"],
            ["sips", "-p", str(box.height), str(box.width), "--padColor", PAD, "scaled.png",
             "--out", "padded.png"],
            ["sips", "-s", "format", "jpeg", "-s", "formatOptions", "best", "padded.png",
             "--out", "flat.jpg"],
            ["sips", "-s", "format", "png", "flat.jpg", "--out", "store.png"],
        ):  # fmt: skip
            code, out = await run(argv, work)
            if code != 0:
                raise RuntimeError(
                    f"sips could not make the store screenshot: {out.strip()[-500:]}"
                )
        made = (work / "store.png").read_bytes()
    if png_size(made) != (box.width, box.height):
        raise RuntimeError(
            f"the store screenshot came out {png_size(made)}, not {box.width}x{box.height}"
        )
    image = ImagePart(media_type="image/png", data_b64=base64.b64encode(made).decode())
    return screen.model_copy(update={"image": image})
