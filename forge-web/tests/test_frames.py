"""Frames and control messages encode, decode and reject what is malformed."""

import asyncio

import pytest

from forge_sandbox.frames import (
    HEADER,
    MAX_PAYLOAD,
    Frame,
    FrameType,
    ProtocolError,
    credit_amount,
    credit_frame,
    read_frame,
    split_payload,
)
from forge_sandbox.protocol import (
    ErrorInfo,
    Hello,
    Notify,
    Request,
    Response,
    from_json,
    parse_control,
    parse_error,
    parse_open,
    to_json,
)


def reader_with(data: bytes) -> asyncio.StreamReader:
    reader = asyncio.StreamReader()
    reader.feed_data(data)
    reader.feed_eof()
    return reader


@pytest.mark.parametrize("kind", list(FrameType))
async def test_round_trip_every_type(kind: FrameType) -> None:
    frame = Frame(7, kind, b"payload" if kind is not FrameType.CREDIT else b"\0\0\0\1")
    decoded = await read_frame(reader_with(frame.encode()))
    assert decoded == frame


async def test_several_frames_then_clean_end() -> None:
    frames = [
        Frame(1, FrameType.DATA, b"a"),
        Frame(0, FrameType.MESSAGE, b"{}"),
        Frame(3, FrameType.CLOSE),
    ]
    reader = reader_with(b"".join(f.encode() for f in frames))
    assert [await read_frame(reader) for _ in frames] == frames
    assert await read_frame(reader) is None


async def test_max_payload_fits_and_more_is_refused() -> None:
    frame = Frame(1, FrameType.DATA, b"x" * MAX_PAYLOAD)
    assert await read_frame(reader_with(frame.encode())) == frame
    with pytest.raises(ValueError):
        Frame(1, FrameType.DATA, b"x" * (MAX_PAYLOAD + 1)).encode()
    with pytest.raises(ProtocolError, match="exceeds"):
        await read_frame(reader_with(HEADER.pack(MAX_PAYLOAD + 1, 1, FrameType.DATA)))


async def test_unknown_type_and_truncation() -> None:
    with pytest.raises(ProtocolError, match="unknown frame type"):
        await read_frame(reader_with(HEADER.pack(0, 1, 99)))
    with pytest.raises(ProtocolError, match="header"):
        await read_frame(reader_with(b"\0\0"))
    with pytest.raises(ProtocolError, match="payload"):
        await read_frame(reader_with(HEADER.pack(5, 1, FrameType.DATA) + b"ab"))


def test_credit_frames() -> None:
    assert credit_amount(credit_frame(5, 4096)) == 4096
    with pytest.raises(ProtocolError):
        credit_amount(Frame(5, FrameType.CREDIT, b"\1"))


def test_split_payload() -> None:
    assert split_payload(b"") == [b""]
    pieces = split_payload(b"x" * (MAX_PAYLOAD * 2 + 3))
    assert [len(p) for p in pieces] == [MAX_PAYLOAD, MAX_PAYLOAD, 3]


@pytest.mark.parametrize(
    "message",
    [
        Hello(role="daemon", version="1", info={"workspace": "/workspace"}),
        Request(id=1, method="fs.read", params={"path": "a.txt"}),
        Response(id=1, ok=True, result={"text": "hi"}),
        Response(id=2, ok=False, error=ErrorInfo(code="not_found", message="no such file")),
        Notify(method="chat.event", params={"seq": 3}),
    ],
)
def test_control_messages_round_trip(message: Hello | Request | Response | Notify) -> None:
    assert parse_control(from_json(to_json(message))) == message


def test_malformed_messages() -> None:
    with pytest.raises(ProtocolError):
        from_json(b"[1, 2]")
    with pytest.raises(ProtocolError):
        from_json(b"{not json")
    with pytest.raises(ProtocolError):
        parse_control({"type": "request", "id": "x"})
    with pytest.raises(ProtocolError):
        parse_open(b'{"args": {}}')
    assert parse_open(b'{"kind": "pty", "args": {"rows": 24}}').kind == "pty"
    assert parse_error(b"garbage").code == "open_failed"
