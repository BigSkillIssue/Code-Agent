"""Turn file bytes into what read_file shows: numbered text, images, PDF text, notebooks."""

import base64
import json
import struct
from pathlib import Path
from typing import Any

from forge.providers.base import ImagePart
from forge.runtime.errors import ToolError
from forge.runtime.files import decode_text, format_size, split_lines
from forge.runtime.proc import run_argv, which

LINE_CUT = 2_000
CHAR_BUDGET = 30_000
MAX_IMAGE_BYTES = 5 * 1024 * 1024
PDF_PAGES_WITHOUT_RANGE = 10
MAX_PDF_PAGES = 20
IMAGE_TYPES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
}


def numbered(lines: list[str], first: int) -> list[str]:
    """`cat -n` style lines: number right-aligned in 6 columns, a tab, then the line."""
    return [f"{first + i:>6}\t{line}" for i, line in enumerate(lines)]


def read_text(display: str, data: bytes, offset: int, limit: int) -> tuple[str, bool]:
    """The numbered view of a text file, and whether every line was returned."""
    decoded = decode_text(data)
    lines = split_lines(decoded.text)
    if not lines:
        return f"file: {display} (empty)", True
    total = len(lines)
    if offset > total:
        raise ToolError("invalid_args", f"offset {offset} is past the end; file has {total} lines")
    out = [f"file: {display} ({total} lines, {format_size(len(data))})"]
    if decoded.latin1:
        out.append("note: file is not valid UTF-8, read as Latin-1")
    used, last = sum(len(line) + 1 for line in out), offset - 1
    for number in range(offset, min(total, offset + limit - 1) + 1):
        line = lines[number - 1]
        if len(line) > LINE_CUT:
            line = line[:LINE_CUT] + " [line truncated]"
        entry = f"{number:>6}\t{line}"
        if used + len(entry) > CHAR_BUDGET and number > offset:
            break
        out.append(entry)
        used, last = used + len(entry) + 1, number
    full = offset == 1 and last == total
    if not full:
        cont = f' Continue with read_file("{display}", offset={last + 1}).' if last < total else ""
        out.append(f"[PARTIAL: lines {offset}-{last} of {total}.{cont}]")
    return "\n".join(out), full


def image_size(data: bytes) -> tuple[int, int] | None:
    """Width and height from a PNG, GIF, JPEG or WebP header, if recognisable."""
    if data[:8] == b"\x89PNG\r\n\x1a\n" and data[12:16] == b"IHDR":
        width, height = struct.unpack(">II", data[16:24])
        return int(width), int(height)
    if data[:6] in (b"GIF87a", b"GIF89a"):
        width, height = struct.unpack("<HH", data[6:10])
        return int(width), int(height)
    if data[:2] == b"\xff\xd8":
        return _jpeg_size(data)
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return _webp_size(data)
    return None


def _jpeg_size(data: bytes) -> tuple[int, int] | None:
    pos = 2
    while pos + 9 < len(data):
        if data[pos] != 0xFF:
            return None
        marker = data[pos + 1]
        length = int.from_bytes(data[pos + 2 : pos + 4], "big")
        if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
            height, width = struct.unpack(">HH", data[pos + 5 : pos + 9])
            return int(width), int(height)
        pos += 2 + length
    return None


def _webp_size(data: bytes) -> tuple[int, int] | None:
    chunk = data[12:16]
    if chunk == b"VP8X":
        width = int.from_bytes(data[24:27], "little") + 1
        height = int.from_bytes(data[27:30], "little") + 1
        return width, height
    if chunk == b"VP8 ":
        width, height = struct.unpack("<HH", data[26:30])
        return int(width) & 0x3FFF, int(height) & 0x3FFF
    if chunk == b"VP8L":
        bits = int.from_bytes(data[21:25], "little")
        return (bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1
    return None


def read_image(display: str, path: Path, data: bytes, vision: bool) -> tuple[str, list[ImagePart]]:
    """The image as an ImagePart for vision models, with a one-line description."""
    if not vision:
        raise ToolError("unsupported", f"{display} is an image and the current model has no vision")
    if len(data) > MAX_IMAGE_BYTES:
        raise ToolError(
            "too_large", f"{display} is {format_size(len(data))}; images are limited to 5 MB"
        )
    size = image_size(data)
    dims = f"{size[0]}x{size[1]}, " if size else ""
    part = ImagePart(
        media_type=IMAGE_TYPES[path.suffix.lower()], data_b64=base64.b64encode(data).decode()
    )
    return f"image: {display} ({dims}{format_size(len(data))})", [part]


async def read_pdf(display: str, path: Path, pages: str | None) -> str:
    """Text of a PDF via `pdftotext -layout`, for at most 20 pages."""
    if which("pdftotext") is None:
        raise ToolError("unsupported", f"cannot read PDF {display}", hint="install poppler-utils")
    count = await _pdf_page_count(path)
    if pages is None and count is not None and count > PDF_PAGES_WITHOUT_RANGE:
        raise ToolError(
            "invalid_args", f"{display} has {count} pages", hint="pass pages, e.g. '1-5'"
        )
    first, last = _page_range(pages, count)
    result = await run_argv(
        ["pdftotext", "-layout", "-f", str(first), "-l", str(last), str(path), "-"], path.parent
    )
    if result.code != 0:
        raise ToolError(
            "unsupported", f"pdftotext failed for {display}", body=result.stderr.strip()
        )
    total = f" of {count}" if count else ""
    return f"file: {display} (PDF, pages {first}-{last}{total})\n{result.stdout.rstrip()}"


async def _pdf_page_count(path: Path) -> int | None:
    if which("pdfinfo") is None:
        return None
    result = await run_argv(["pdfinfo", str(path)], path.parent)
    for line in result.stdout.splitlines():
        if line.startswith("Pages:"):
            return int(line.split()[1])
    return None


def _page_range(pages: str | None, count: int | None) -> tuple[int, int]:
    if pages is None:
        return 1, count or PDF_PAGES_WITHOUT_RANGE
    try:
        first_text, _, last_text = pages.partition("-")
        first, last = int(first_text), int(last_text or first_text)
    except ValueError as exc:
        raise ToolError(
            "invalid_args", f"pages must look like '3' or '1-5', got '{pages}'"
        ) from exc
    if first < 1 or last < first or last - first + 1 > MAX_PDF_PAGES:
        raise ToolError("invalid_args", "pages must be a range of 1 to 20 pages, like '1-5'")
    return first, last


def read_notebook(display: str, data: bytes) -> str:
    """Each cell of a Jupyter notebook with its source and text outputs."""
    try:
        notebook = json.loads(data)
    except json.JSONDecodeError as exc:
        raise ToolError("invalid_args", f"{display} is not valid notebook JSON: {exc}") from exc
    cells = notebook.get("cells", [])
    out = [f"file: {display} (notebook, {len(cells)} cells, {format_size(len(data))})"]
    for index, cell in enumerate(cells):
        kind = cell.get("cell_type", "code")
        out.append(f"--- cell {index} [{kind}] id={cell.get('id', '-')} ---")
        out.append(_joined(cell.get("source", "")).rstrip("\n"))
        if kind == "code" and cell.get("outputs"):
            out.append("--- output ---")
            out.extend(_output_text(o) for o in cell["outputs"])
    return "\n".join(out)


def _joined(value: Any) -> str:
    return "".join(value) if isinstance(value, list) else str(value)


def _output_text(output: dict[str, Any]) -> str:
    kind = output.get("output_type")
    if kind == "stream":
        return _joined(output.get("text", "")).rstrip("\n")
    if kind == "error":
        return f"{output.get('ename', 'Error')}: {output.get('evalue', '')}"
    data = output.get("data", {})
    if "text/plain" in data:
        return _joined(data["text/plain"]).rstrip("\n")
    return "[image output]" if any(k.startswith("image/") for k in data) else "[output]"
