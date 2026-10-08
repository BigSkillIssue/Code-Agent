"""Chats in the daemon: real worker processes, numbered buffers, replay, first answer wins."""

import asyncio
from collections import deque
from pathlib import Path
from typing import Any

import pytest

from forge_sandbox.chats import ITEM_LIMIT, Chat, fit
from forge_sandbox.mux import Channel, ChannelClosed, OpenFailed
from forge_sandbox.rpc import RpcError
from support import call, fake_script, sandbox


class Follower:
    """Reads a chat channel in the background and lets a test wait for items."""

    def __init__(self, channel: Channel) -> None:
        self.channel = channel
        self.messages: list[dict[str, Any]] = []
        self._new = asyncio.Event()
        self.task = asyncio.create_task(self._read())

    async def _read(self) -> None:
        try:
            while True:
                self.messages.append(await self.channel.recv_message())
                self._new.set()
        except ChannelClosed:
            pass

    def items(self) -> list[tuple[int, dict[str, Any]]]:
        return [(m["seq"], m["item"]) for m in self.messages if m.get("type") == "item"]

    async def wait_for(
        self, item_type: str, timeout: float = 30, after: int = 0
    ) -> tuple[int, dict[str, Any]]:
        async with asyncio.timeout(timeout):
            while True:
                for seq, item in self.items():
                    if seq > after and item.get("type") == item_type:
                        return seq, item
                self._new.clear()
                await self._new.wait()


@pytest.fixture
def env(tmp_path: Path) -> dict[str, str]:
    return {"FORGE_HOME": str(tmp_path / "forge-home")}


async def test_chat_round_trip_and_replay_from_any_point(
    tmp_path: Path, env: dict[str, str]
) -> None:
    root = tmp_path / "ws"
    root.mkdir()
    async with sandbox(root, env=env) as (_daemon, client):
        options = {"fake_script": fake_script({"text": "Hello through the daemon."})}
        opened = await client.call("chat.open", {"chat_id": "c1", "options": options})
        assert opened["state"] == "starting"
        live = Follower(await client.open("chat", {"chat_id": "c1"}))
        await live.wait_for("ready")
        await client.call("chat.send", {"chat_id": "c1", "text": "Say hello"})
        turn_seq, turn = await live.wait_for("turn")
        assert turn["ok"] and "Hello through the daemon." in turn["summary"]
        await live.wait_for("status", after=turn_seq)  # idle again: nothing else follows
        seqs = [seq for seq, _ in live.items()]
        assert seqs == list(range(1, len(seqs) + 1))  # numbered without gaps
        middle = seqs[len(seqs) // 2]
        replay = Follower(await client.open("chat", {"chat_id": "c1", "after_seq": middle}))
        await asyncio.sleep(0.3)
        assert replay.messages[0]["type"] == "hello"
        assert [seq for seq, _ in replay.items()] == [s for s in seqs if s > middle]
        listed = await client.call("chat.list")
        assert listed[0]["chat_id"] == "c1" and listed[0]["state"] == "idle"
        await client.call("chat.close", {"chat_id": "c1"})


async def test_first_answer_wins_and_a_crashed_worker_is_replaced(
    tmp_path: Path, env: dict[str, str]
) -> None:
    root = tmp_path / "ws"
    root.mkdir()
    script = fake_script(call("write_file", path="a.txt", content="A\n"), {"text": "Done."})
    async with sandbox(root, env=env) as (daemon, client):
        options = {"mode": "ask", "fake_script": script}
        await client.call("chat.open", {"chat_id": "c2", "options": options})
        live = Follower(await client.open("chat", {"chat_id": "c2"}))
        _, ready = await live.wait_for("ready")
        await client.call("chat.send", {"chat_id": "c2", "text": "Write a.txt"})
        with pytest.raises(RpcError) as busy:
            await client.call("chat.send", {"chat_id": "c2", "text": "again"})
        assert busy.value.code == "busy"
        _, request = await live.wait_for("request")
        # The worker dies while the approval is open: the request is withdrawn.
        process = daemon.chats.chats["c2"].process
        assert process is not None
        process.kill()
        exited, _ = await live.wait_for("worker_exited")
        resolved = [i for _, i in live.items() if i.get("type") == "request_resolved"]
        assert resolved == [{"type": "request_resolved", "id": request["id"], "cancelled": True}]
        # Sending again starts a new worker on the same Forge session.
        await client.call("chat.send", {"chat_id": "c2", "text": "Write a.txt"})
        _, ready_again = await live.wait_for("ready", after=exited)
        assert ready_again["session_id"] == ready["session_id"]
        seq, request = await live.wait_for("request", after=exited)
        answer = {"chat_id": "c2", "request_id": request["id"], "answer": {"allow": True}}
        first, second = await asyncio.gather(
            client.call("chat.answer", answer), client.call("chat.answer", answer)
        )
        assert sorted([first["accepted"], second["accepted"]]) == [False, True]
        _, turn = await live.wait_for("turn", after=seq)
        assert turn["ok"] and (root / "a.txt").read_text() == "A\n"


async def test_attach_reports_gaps_and_unknown_chats(tmp_path: Path) -> None:
    async with sandbox(tmp_path) as (daemon, client):
        chat = Chat("old")
        chat.buffer = deque(maxlen=3)
        for n in range(6):
            chat.append({"type": "event", "event": {"kind": "model_delta", "text": str(n)}})
        daemon.chats.chats["old"] = chat
        follower = Follower(await client.open("chat", {"chat_id": "old", "after_seq": 1}))
        await asyncio.sleep(0.2)
        assert follower.messages[0]["type"] == "hello" and follower.messages[0]["oldest"] == 4
        assert follower.messages[1] == {"type": "gap", "from": 2}
        assert [seq for seq, _ in follower.items()] == [4, 5, 6]
        with pytest.raises(OpenFailed) as unknown:
            await client.open("chat", {"chat_id": "nope"})
        assert unknown.value.info.code == "not_found"
        with pytest.raises(RpcError) as not_open:
            await client.call("chat.send", {"chat_id": "nope", "text": "hi"})
        assert not_open.value.code == "not_found"
        with pytest.raises(RpcError) as bad_id:
            await client.call("chat.open", {"chat_id": "../x"})
        assert bad_id.value.code == "bad_params"


def test_worker_messages_are_checked_and_fitted() -> None:
    from forge_sandbox.chats import Chats
    from forge_sandbox.fsops import Workspace

    chats = Chats(Workspace(Path.cwd()), {})
    chat = Chat("c")
    chats._received(chat, {"type": "surprise"})
    chats._received(chat, ["not", "a", "dict"])
    chats._received(chat, {"type": "request", "id": 5, "kind": "approval"})
    chats._received(chat, {"type": "request", "id": "r1", "kind": "steal"})
    assert chat.seq == 0 and chat.pending == {}
    chats._received(chat, {"type": "request", "id": "r1", "kind": "approval", "payload": {}})
    assert list(chat.pending) == ["r1"] and chat.seq == 1
    huge = {
        "type": "event",
        "event": {"kind": "tool_finished", "result": {"text": "x" * 2_000_000}},
    }
    small = fit(huge)
    assert small["event"]["result"]["text"].startswith("[omitted:")
    many = {"type": "event", "event": {"kind": "model_done", "parts": ["y" * 90_000] * 20}}
    assert fit(many)["type"] == "oversized" and len(str(fit(many))) < ITEM_LIMIT
