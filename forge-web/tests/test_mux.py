"""The multiplexer: handshake, channels, flow control, and a hostile peer."""

import asyncio
import contextlib
from typing import Any

import pytest

from forge_sandbox import mux as mux_module
from forge_sandbox import rpc as rpc_module
from forge_sandbox.frames import HEADER, Frame, FrameType, ProtocolError, read_frame
from forge_sandbox.mux import Channel, ChannelClosed, Mux, OpenFailed, OpenRefused, ProtocolMismatch
from forge_sandbox.protocol import Hello, OpenRequest, to_json
from forge_sandbox.rpc import Rpc, RpcError
from support import mux_pair, stream_pair


async def echo_handler(channel: Channel) -> None:
    """Accept 'echo' channels and send back whatever arrives; refuse everything else."""
    if channel.kind != "echo":
        raise OpenRefused("unknown_kind", f"no channel kind {channel.kind}")
    await channel.accept()
    while True:
        try:
            item = await channel.receive()
        except ChannelClosed:
            return
        if isinstance(item, bytes):
            await channel.send(item)
        else:
            await channel.send_message(item)


async def test_handshake_exchanges_hello() -> None:
    async with mux_pair() as (server, daemon):
        assert server.peer is not None and server.peer.role == "daemon"
        assert server.peer.info == {"name": "test"}
        assert daemon.peer is not None and daemon.peer.role == "server"


async def test_bytes_and_messages_round_trip() -> None:
    async with mux_pair(echo_handler) as (server, _daemon):
        channel = await server.open("echo", {"x": 1})
        await channel.send(b"hello")
        assert await channel.read() == b"hello"
        await channel.send_message({"text": "hi", "n": [1, 2]})
        assert await channel.recv_message() == {"text": "hi", "n": [1, 2]}


async def test_large_message_is_fragmented_and_reassembled() -> None:
    async with mux_pair(echo_handler) as (server, _daemon):
        channel = await server.open("echo")
        big = {"blob": "x" * 500_000}
        await channel.send_message(big)
        assert await channel.recv_message() == big


async def test_message_over_limit_is_refused_before_sending() -> None:
    async with mux_pair(echo_handler, window=64 * 1024) as (server, _daemon):
        channel = await server.open("echo")
        with pytest.raises(ValueError, match="exceeds"):
            await channel.send_message({"blob": "x" * 70_000})


async def test_message_larger_than_rest_of_window_does_not_deadlock() -> None:
    async with mux_pair(echo_handler, window=128 * 1024) as (server, _daemon):
        channel = await server.open("echo")
        for size in (40_000, 100_000, 120_000, 90_000):
            await channel.send_message({"blob": "y" * size})
            reply = await asyncio.wait_for(channel.recv_message(), 5)
            assert len(reply["blob"]) == size


async def test_open_refused_and_unknown_kind() -> None:
    async with mux_pair(echo_handler) as (server, daemon):
        with pytest.raises(OpenFailed) as failed:
            await server.open("nope")
        assert failed.value.info.code == "unknown_kind"
        with pytest.raises(OpenFailed) as no_handler:
            await daemon.open("echo")  # the server side opens no channels
        assert no_handler.value.info.code == "not_supported"


async def test_close_ends_the_peer_side() -> None:
    received: asyncio.Queue[bytes] = asyncio.Queue()

    async def reader(channel: Channel) -> None:
        await channel.accept()
        while chunk := await channel.read():
            received.put_nowait(chunk)
        received.put_nowait(b"<eof>")

    async with mux_pair(reader) as (server, _daemon):
        channel = await server.open("any")
        await channel.send(b"one")
        await channel.close()
        assert await received.get() == b"one"
        assert await asyncio.wait_for(received.get(), 5) == b"<eof>"


async def test_stalled_channel_blocks_neither_control_nor_other_channels() -> None:
    async def handler(channel: Channel) -> None:
        await channel.accept()
        if channel.kind == "sink":
            await asyncio.sleep(3600)  # never reads
        await echo_handler(channel)

    async def ping(_params: dict[str, Any]) -> str:
        return "pong"

    async with mux_pair(handler, window=64 * 1024) as (server, daemon):
        rpc_server, rpc_daemon = Rpc(server.control), Rpc(daemon.control, {"ping": ping})
        loops = [asyncio.create_task(rpc_server.run()), asyncio.create_task(rpc_daemon.run())]
        sink = await server.open("sink")
        flood = asyncio.create_task(sink.send(b"z" * (64 * 1024 * 4)))
        await asyncio.sleep(0.2)
        assert not flood.done()  # out of credit: waits instead of buffering without limit
        assert await asyncio.wait_for(rpc_server.call("ping"), 5) == "pong"
        other = await server.open("echo")
        await other.send(b"still flowing")
        assert await asyncio.wait_for(other.read(), 5) == b"still flowing"
        flood.cancel()
        for loop in loops:
            loop.cancel()


async def test_rpc_results_errors_and_notifications() -> None:
    notes: asyncio.Queue[str] = asyncio.Queue()

    async def add(params: dict[str, Any]) -> int:
        return int(params["a"]) + int(params["b"])

    async def fail(_params: dict[str, Any]) -> None:
        raise RpcError("not_found", "no such file")

    async def crash(_params: dict[str, Any]) -> None:
        raise RuntimeError("secret detail")

    async def on_notify(message: Any) -> None:
        notes.put_nowait(message.method)

    async with mux_pair() as (server, daemon):
        caller = Rpc(server.control)
        callee = Rpc(daemon.control, {"add": add, "fail": fail, "crash": crash}, on_notify)
        loops = [asyncio.create_task(caller.run()), asyncio.create_task(callee.run())]
        assert await caller.call("add", {"a": 2, "b": 3}) == 5
        with pytest.raises(RpcError) as err:
            await caller.call("fail")
        assert err.value.code == "not_found"
        with pytest.raises(RpcError) as crashed:
            await caller.call("crash")
        assert crashed.value.code == "internal" and "secret" not in crashed.value.message
        with pytest.raises(RpcError) as unknown:
            await caller.call("missing")
        assert unknown.value.code == "unknown_method"
        await caller.notify("chat.event", {"seq": 1})
        assert await asyncio.wait_for(notes.get(), 5) == "chat.event"
        for loop in loops:
            loop.cancel()


async def test_connection_loss_fails_waiting_calls_and_channels() -> None:
    async with mux_pair(echo_handler) as (server, daemon):
        caller = Rpc(server.control)
        loop = asyncio.create_task(caller.run())
        channel = await server.open("echo")
        pending = asyncio.create_task(caller.call("never-answered"))
        await asyncio.sleep(0.05)
        await daemon.close()
        with pytest.raises(ChannelClosed):
            await asyncio.wait_for(pending, 5)
        with pytest.raises(ChannelClosed):
            await asyncio.wait_for(channel.receive(), 5)
        await asyncio.wait_for(server.wait_closed(), 5)
        loop.cancel()


async def test_too_many_channels() -> None:
    (sr, sw), (dr, dw) = await stream_pair()
    server = Mux(sr, sw, role="server")
    daemon = Mux(dr, dw, role="daemon", on_open=echo_handler, max_channels=3)
    await asyncio.gather(server.start(), daemon.start())
    channels = [await server.open("echo"), await server.open("echo")]
    with pytest.raises(OpenFailed) as failed:
        await server.open("echo")
    assert failed.value.info.code == "too_many_channels"
    assert len(channels) == 2
    await server.close()
    await daemon.close()


# A hostile peer writes raw frames ------------------------------------------------------------


async def hold_or_echo(channel: Channel) -> None:
    """'hold' channels are accepted and never read (so no credit comes back); others echo."""
    if channel.kind != "hold":
        await echo_handler(channel)
        return
    await channel.accept()
    await asyncio.Event().wait()


async def raw_peer() -> tuple[Mux, asyncio.StreamReader, asyncio.StreamWriter]:
    """A daemon-side Mux whose server side the test drives frame by frame."""
    (sr, sw), (dr, dw) = await stream_pair()
    daemon = Mux(dr, dw, role="daemon", on_open=hold_or_echo, window=64 * 1024)
    started = asyncio.create_task(daemon.start())
    sw.write(Frame(0, FrameType.MESSAGE, to_json(Hello(role="server", version="t"))).encode())
    await started
    assert await read_frame(sr) is not None  # the daemon's hello
    return daemon, sr, sw


async def test_version_mismatch_is_reported() -> None:
    (_sr, sw), (dr, dw) = await stream_pair()
    daemon = Mux(dr, dw, role="daemon")
    sw.write(
        Frame(
            0, FrameType.MESSAGE, to_json(Hello(role="server", version="t", protocol=99))
        ).encode()
    )
    with pytest.raises(ProtocolMismatch, match="99"):
        await daemon.start()
    assert daemon.closed


async def test_peer_exceeding_its_window_is_cut_off() -> None:
    daemon, sr, sw = await raw_peer()
    # The channel's reader never reads, so the daemon grants nothing beyond the first 64 KiB
    # (with a reader that keeps up, credit would come back while the bytes arrive).
    sw.write(Frame(1, FrameType.OPEN, to_json(OpenRequest(kind="hold"))).encode())
    opened = await read_frame(sr)
    while opened is not None and opened.type is FrameType.CREDIT:
        opened = await read_frame(sr)
    assert opened is not None and opened.type is FrameType.OPEN_OK
    for _ in range(2):  # 2 x 64 KiB without waiting for credit
        sw.write(Frame(1, FrameType.DATA, b"q" * 65536).encode())
    await asyncio.wait_for(daemon.wait_closed(), 5)
    assert isinstance(daemon.error, ProtocolError)


async def test_oversized_frame_header_closes_the_connection() -> None:
    daemon, _sr, sw = await raw_peer()
    sw.write(HEADER.pack(10_000_000, 1, FrameType.DATA))
    await asyncio.wait_for(daemon.wait_closed(), 5)
    assert isinstance(daemon.error, ProtocolError)


async def test_open_with_wrong_parity_is_a_protocol_error() -> None:
    daemon, _sr, sw = await raw_peer()
    sw.write(Frame(2, FrameType.OPEN, to_json(OpenRequest(kind="echo"))).encode())
    await asyncio.wait_for(daemon.wait_closed(), 5)
    assert isinstance(daemon.error, ProtocolError)


async def test_garbage_message_is_a_protocol_error() -> None:
    daemon, _sr, sw = await raw_peer()
    sw.write(Frame(0, FrameType.MESSAGE, b"\xff\xfe not json").encode())
    rpc = Rpc(daemon.control)
    await asyncio.wait_for(rpc.run(), 5)  # ends instead of crashing


async def test_a_peer_that_never_reads_is_cut_off(monkeypatch: pytest.MonkeyPatch) -> None:
    # Every refused OPEN costs an answer that needs no credit: a peer that sends them without
    # reading would otherwise make the queue grow for ever.
    monkeypatch.setattr(mux_module, "MAX_URGENT", 50)
    daemon, _sr, sw = await raw_peer()
    with contextlib.suppress(ConnectionError):
        for n in range(20_000):
            sw.write(Frame(1 + 2 * n, FrameType.OPEN, to_json(OpenRequest(kind="nope"))).encode())
            if n % 500 == 0:
                await sw.drain()
    await asyncio.wait_for(daemon.wait_closed(), 10)
    assert isinstance(daemon.error, ProtocolError) and "stopped reading" in str(daemon.error)


async def test_requests_beyond_the_limit_wait_unread(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(rpc_module, "MAX_TASKS", 5)
    later = asyncio.Event()

    async def slow(_params: dict[str, Any]) -> str:
        await later.wait()
        return "done"

    async with mux_pair() as (server, daemon):
        caller = Rpc(server.control)
        callee = Rpc(daemon.control, {"slow": slow})
        loops = [asyncio.create_task(caller.run()), asyncio.create_task(callee.run())]
        calls = [asyncio.create_task(caller.call("slow", timeout=30)) for _ in range(12)]
        await asyncio.sleep(0.3)
        assert len(callee._tasks) == 5  # the rest waits in the peer's window, unread
        later.set()
        assert await asyncio.gather(*calls) == ["done"] * 12
        for loop in loops:
            loop.cancel()
