"""The Windows app of a full-stack product (S72): WinUI 3 on .NET 10 from `templates/windows/`,
packaged as MSIX (the Microsoft Store signs it), with a client of the server's API that Kiota
writes from its snapshot on every build, sign-in and account deletion in the app, and Microsoft
Store purchases when the app sells digital goods. It builds on Windows.

The template's text files are rendered like the full-stack template's; the MSIX's logos are
drawn here as plain PNGs in the app's colour, so no binary file sits in the templates.
"""

import re
import struct
import zlib

KIND = "windows"
COLOUR = (0x25, 0x63, 0xEB)  # the logos' blue, until the maker draws their own
LOGOS = {  # the files Package.appxmanifest names, with the scales Windows asks for
    "Assets/StoreLogo.png": (50, 50),
    "Assets/Square150x150Logo.scale-200.png": (300, 300),
    "Assets/Square44x44Logo.scale-200.png": (88, 88),
    "Assets/Square44x44Logo.targetsize-24_altform-unplated.png": (24, 24),
    "Assets/Wide310x150Logo.scale-200.png": (620, 300),
    "Assets/SplashScreen.scale-200.png": (1240, 600),
    "Assets/LockScreenLogo.scale-200.png": (48, 48),
}


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


def logos() -> dict[str, bytes]:
    """The app's logos, by their path in the app's project."""
    return {path: logo(*size) for path, size in LOGOS.items()}
