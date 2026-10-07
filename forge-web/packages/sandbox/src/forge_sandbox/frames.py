"""Binary frames: the unit everything between the server and a sandbox daemon travels in.

A frame is a 9-byte header — payload length (u32), channel id (u32), frame type (u8), all big
endian — followed by at most MAX_PAYLOAD bytes of payload.
"""

import asyncio
import struct
from dataclasses import dataclass
from enum import IntEnum

HEADER = struct.Struct(">IIB")
MAX_PAYLOAD = 64 * 1024
CONTROL_CHANNEL = 0


class FrameType(IntEnum):
    """What a frame carries."""

    DATA = 1  # raw bytes of a byte-stream channel
    MESSAGE = 2  # the last (or only) part of one JSON message
    MESSAGE_MORE = 3  # a part of a JSON message, more parts follow
    OPEN = 4  # open a channel; payload: JSON OpenRequest
    OPEN_OK = 5  # the channel is open
    OPEN_FAIL = 6  # the channel could not be opened; payload: JSON ErrorInfo
    CLOSE = 7  # no more frames on this channel from the sender
    CREDIT = 8  # payload: u32, how many more payload bytes the peer may send


# Frames that manage the connection jump the queue and need no credit. CLOSE is not one of them:
# it must stay behind the data the channel sent before it.
MANAGEMENT = frozenset({FrameType.OPEN, FrameType.OPEN_OK, FrameType.OPEN_FAIL, FrameType.CREDIT})
CREDIT_FORMAT = struct.Struct(">I")


class ProtocolError(Exception):
    """The peer broke the protocol; the connection must be closed."""


@dataclass(frozen=True)
class Frame:
    """One decoded frame."""

    channel: int
    type: FrameType
    payload: bytes = b""

    def encode(self) -> bytes:
        """The frame as bytes on the wire."""
        if len(self.payload) > MAX_PAYLOAD:
            raise ValueError(f"payload of {len(self.payload)} bytes exceeds {MAX_PAYLOAD}")
        return HEADER.pack(len(self.payload), self.channel, self.type) + self.payload


def credit_frame(channel: int, amount: int) -> Frame:
    """A CREDIT frame granting `amount` more payload bytes on `channel`."""
    return Frame(channel, FrameType.CREDIT, CREDIT_FORMAT.pack(amount))


def credit_amount(frame: Frame) -> int:
    """The number of bytes a CREDIT frame grants."""
    if len(frame.payload) != CREDIT_FORMAT.size:
        raise ProtocolError("malformed CREDIT frame")
    (amount,) = CREDIT_FORMAT.unpack(frame.payload)
    return int(amount)


def decode_header(header: bytes) -> tuple[int, int, FrameType]:
    """(payload length, channel, type) of a header; ProtocolError if it is not valid."""
    length, channel, raw_type = HEADER.unpack(header)
    if length > MAX_PAYLOAD:
        raise ProtocolError(f"frame of {length} bytes exceeds the {MAX_PAYLOAD}-byte limit")
    try:
        kind = FrameType(raw_type)
    except ValueError:
        raise ProtocolError(f"unknown frame type {raw_type}") from None
    return length, channel, kind


async def read_frame(reader: asyncio.StreamReader) -> Frame | None:
    """The next frame, or None at a clean end of stream (between frames)."""
    try:
        header = await reader.readexactly(HEADER.size)
    except asyncio.IncompleteReadError as err:
        if not err.partial:
            return None
        raise ProtocolError("stream ended inside a frame header") from None
    length, channel, kind = decode_header(header)
    try:
        payload = await reader.readexactly(length) if length else b""
    except asyncio.IncompleteReadError:
        raise ProtocolError("stream ended inside a frame payload") from None
    return Frame(channel, kind, payload)


def split_payload(data: bytes) -> list[bytes]:
    """`data` cut into pieces that each fit one frame (at least one piece)."""
    if not data:
        return [b""]
    return [data[i : i + MAX_PAYLOAD] for i in range(0, len(data), MAX_PAYLOAD)]
