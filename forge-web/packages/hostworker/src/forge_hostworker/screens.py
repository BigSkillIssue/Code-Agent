"""Screenshots for the agent (W61): their size, read from the PNG, and their base64 text."""

import base64
import struct

PNG = b"\x89PNG\r\n\x1a\n"


def png_size(data: bytes) -> tuple[int, int]:
    """A PNG's width and height (0, 0 when it is none)."""
    if len(data) < 24 or not data.startswith(PNG) or data[12:16] != b"IHDR":
        return 0, 0
    width, height = struct.unpack(">II", data[16:24])
    return int(width), int(height)


def encoded(data: bytes) -> str:
    """The picture as base64 text."""
    return base64.b64encode(data).decode("ascii")
