"""Files and images the user attached to a task's message (S75): checked before the task
starts, listed for every agent of the task, and images shown to models that can see."""

import asyncio
import base64
import mimetypes
from collections.abc import Sequence
from pathlib import Path

from pydantic import BaseModel

from forge.providers.base import ImagePart
from forge.runtime.files import display_path, format_size, resolve_path
from forge.runtime.readers import IMAGE_TYPES, MAX_IMAGE_BYTES, image_size

MAX_ATTACHMENTS = 10
# mimetypes without the system's tables, so the same name gets the same type on every OS
KNOWN_TYPES = mimetypes.MimeTypes()
MORE_TYPES = {".md": "text/markdown"}


class Attachment(BaseModel):
    """A file attached to a message: where it is and what it is."""

    path: str  # relative to the project root, or absolute
    name: str = ""  # as the user called it; the file's own name when empty
    media_type: str = ""  # guessed from the file's name when empty
    size: int = 0  # bytes; measured when the attachment is checked


class AttachmentError(Exception):
    """An attachment that cannot be used: missing, a folder, or one too many."""


def media_type(path: Path) -> str:
    """The media type for a file name (images always by their suffix)."""
    suffix = path.suffix.lower()
    if suffix in IMAGE_TYPES:
        return IMAGE_TYPES[suffix]
    known = MORE_TYPES.get(suffix) or KNOWN_TYPES.guess_type(path.name)[0]
    return known or "application/octet-stream"


def attachment_for(cwd: Path, path: str) -> Attachment:
    """An attachment for a file named on the command line, relative to `cwd` (not checked yet)."""
    target = resolve_path(cwd, path)
    size = target.stat().st_size if target.is_file() else 0
    return Attachment(path=str(target), name=target.name, media_type=media_type(target), size=size)


async def check_attachments(root: Path, given: Sequence[Attachment]) -> list[Attachment]:
    """The attachments with name, type and size from the files; raises AttachmentError."""
    if len(given) > MAX_ATTACHMENTS:
        raise AttachmentError(f"{len(given)} files attached; at most {MAX_ATTACHMENTS} are allowed")
    return [await asyncio.to_thread(_checked, root, one) for one in given]


def _checked(root: Path, one: Attachment) -> Attachment:
    target = resolve_path(root, one.path)
    if not target.is_file():
        raise AttachmentError(f"the attached file {one.path} does not exist")
    size = target.stat().st_size
    kind = IMAGE_TYPES.get(target.suffix.lower()) or one.media_type or media_type(target)
    small_image = kind in IMAGE_TYPES.values() and size <= MAX_IMAGE_BYTES
    if small_image and image_size(target.read_bytes()) is None:  # only named like an image
        kind = "application/octet-stream"
    name = one.name or target.name
    return Attachment(path=display_path(root, target), name=name, media_type=kind, size=size)


def can_show(one: Attachment) -> bool:
    """True for an image small enough to put into a message (the 5 MB of read_file)."""
    return one.media_type in IMAGE_TYPES.values() and one.size <= MAX_IMAGE_BYTES


async def image_parts(root: Path, attachments: Sequence[Attachment]) -> list[ImagePart]:
    """The attached images that fit into a message, as image parts."""
    parts: list[ImagePart] = []
    for one in filter(can_show, attachments):
        data = await asyncio.to_thread(resolve_path(root, one.path).read_bytes)
        parts.append(ImagePart(media_type=one.media_type, data_b64=base64.b64encode(data).decode()))
    return parts


def listing(attachments: Sequence[Attachment], shown: bool) -> str:
    """One line per attachment: its path, name, type and size, and whether it is shown."""
    lines = []
    for one in attachments:
        seen = " (shown with this message)" if shown and can_show(one) else ""
        lines.append(f"- {one.path} ({one.name}, {one.media_type}, {format_size(one.size)}){seen}")
    return "\n".join(lines)
