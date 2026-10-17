"""The Windows app of a full-stack product (S72, S73): WinUI 3 on .NET 10 from
`templates/windows/`, with a client of the server's API that Kiota writes from its snapshot on
every build, sign-in and account deletion in the app. People download it from the product's
website, as an installer or a ZIP to run without installing; Velopack makes both and updates the
app from the website. It builds on Windows.

The template's text files are rendered like the full-stack template's; the app's icon is drawn
here as plain PNGs in an ICO in the app's colour, so no binary file sits in the templates.
"""

import re
import struct
import zlib

KIND = "windows"
COLOUR = (0x25, 0x63, 0xEB)  # the icon's blue, until the maker draws their own
ICON = "App/App.ico"
ICON_SIZES = (16, 24, 32, 48, 256)  # what Explorer, the taskbar and the installer show


def namespace_of(title: str) -> str:
    """The app's C# namespace and package name: "Tally Notes" → "TallyNotes"."""
    words = [part for part in re.split(r"[^A-Za-z0-9]+", title) if part]
    name = "".join(word[:1].upper() + word[1:] for word in words)[:40]
    return name if name[:1].isalpha() else f"App{name}"[:40]


def chunk(kind: bytes, data: bytes) -> bytes:
    """One PNG chunk: its length, kind, data and checksum."""
    return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))


def logo(width: int, height: int) -> bytes:
    """A PNG of one colour (RGB, 8 bits per channel)."""
    row = b"\x00" + bytes(COLOUR) * width  # filter type 0, then the row's pixels
    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header)
            + chunk(b"IDAT", zlib.compress(row * height, 9)) + chunk(b"IEND", b""))  # fmt: skip


def icon() -> bytes:
    """The app's icon: an ICO with one PNG per size (Windows reads PNGs in icons)."""
    images = [logo(size, size) for size in ICON_SIZES]
    entries, offset = b"", 6 + 16 * len(images)
    for size, image in zip(ICON_SIZES, images, strict=True):
        # width and height 0 mean 256; one plane, 24 bits per pixel; where the image is
        entries += struct.pack("<BBBBHHII", size % 256, size % 256, 0, 0, 1, 24, len(image), offset)
        offset += len(image)
    return struct.pack("<HHH", 0, 1, len(images)) + entries + b"".join(images)
