"""A chat's Forge worker: turns, approvals, questions, cancel, follow-ups, picking up again.

These run the worker in the test process (fast); test_chats.py runs real worker processes.
"""

import asyncio
from pathlib import Path
from typing import Any

import pytest

from forge_sandbox.history import ChatState, Turn, with_history
from forge_sandbox.methods import ChatOptions
from forge_sandbox.worker import AUTO_ALLOW, ChatWorker, build_config, config_overrides
from support import call, fake_script


class Collector:
    """Stands in for the worker's outbox and lets a test wait for messages."""

    def __init__(self) -> None:
        self.messages: list[dict[str, Any]] = []
        self._new = asyncio.Event()

    async def send(self, message: dict[str, Any]) -> None:
        self.messages.append(message)
        self._new.set()

    async def wait_for(
        self, message_type: str, timeout: float = 15, **match: Any
    ) -> dict[str, Any]:
        """The first message of this type (and these fields) not yet returned."""
        seen = 0
        async with asyncio.timeout(timeout):
            while True:
                for message in self.messages[seen:]:
                    seen += 1
                    if message.get("type") == message_type and all(
                        message.get(k) == v for k, v in match.items()
                    ):
                        self.messages.remove(message)
                        return message
                self._new.clear()
                await self._new.wait()

    def events(self) -> list[dict[str, Any]]:
        return [m["event"] for m in self.messages if m.get("type") == "event"]


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "forge-home"
    monkeypatch.setenv("FORGE_HOME", str(path))
    return path


@pytest.fixture
def root(tmp_path: Path) -> Path:
    path = tmp_path / "workspace"
    path.mkdir()
    return path


async def started(
    root: Path, options: ChatOptions, chat_id: str = "c1"
) -> tuple[ChatWorker, Collector]:
    out = Collector()
    worker = ChatWorker(root, chat_id, options, out)  # type: ignore[arg-type]
    await worker.start()
    await out.wait_for("ready")
    return worker, out


def test_config_overrides_per_mode() -> None:
    ask = config_overrides(ChatOptions(mode="ask"))
    assert ask["approval.policy"] == "always" and ask["permissions.allow"] == []
    edits = config_overrides(ChatOptions(mode="edits"))
    assert edits["approval.policy"] == "on-request"
    auto = config_overrides(ChatOptions(mode="auto", model="openai/gpt-x", max_cost_usd=2))
    assert auto["approval.policy"] == "never" and auto["permissions.allow"] == AUTO_ALLOW
    assert auto["roles"]["coder"] == ["openai/gpt-x"] and auto["limits.max_cost_usd"] == 2
    gateway = {"kind": "anthropic", "base_url": "http://127.0.0.1:1/anthropic"}
    assert config_overrides(ChatOptions(providers={"gw": gateway}))["providers.gw"] == gateway


def test_project_config_cannot_loosen_approvals(root: Path, home: Path) -> None:
    (root / ".forge").mkdir()
    (root / ".forge" / "config.toml").write_text(
        '[approval]\npolicy = "never"\n[permissions]\nallow = ["bash"]\n'
        '[sandbox]\nmode = "full-access"\n'
    )
    cfg = build_config(root, ChatOptions(mode="ask"))
    assert cfg.approval.policy == "always"
    assert cfg.permissions.allow == []
    assert cfg.sandbox.mode == "workspace-write"


async def test_turn_streams_events_and_a_report(root: Path, home: Path) -> None:
    script = fake_script({"text": "Hello from the fake model."})
    worker, out = await started(root, ChatOptions(fake_script=script))
    assert worker.submit("Say hello")
    turn = await out.wait_for("turn")
    assert turn["ok"] and "Hello from the fake model." in turn["summary"]
    kinds = [e["kind"] for e in out.events()]
    assert "model_delta" in kinds and "model_done" in kinds
    statuses = [m["state"] for m in out.messages if m.get("type") == "status"]
    assert statuses[:1] == ["running"]
    await worker.close()


async def test_approval_round_trip_allows_the_change(root: Path, home: Path) -> None:
    script = fake_script(
        call("write_file", path="hello.txt", content="hi\n"), {"text": "Wrote it."}
    )
    worker, out = await started(root, ChatOptions(mode="ask", fake_script=script))
    worker.submit("Create hello.txt")
    request = await out.wait_for("request", kind="approval")
    assert request["payload"]["call"]["name"] == "write_file"
    assert worker.renderer.resolve(request["id"], {"allow": True})
    turn = await out.wait_for("turn")
    assert turn["ok"] and (root / "hello.txt").read_text() == "hi\n"
    assert not worker.renderer.resolve(request["id"], {"allow": True})  # already answered
    await worker.close()


@pytest.mark.parametrize(
    "answer", [{"allow": False, "feedback": "not now"}, {"allow": "maybe"}, "x"]
)
async def test_refused_or_malformed_approval_changes_nothing(
    root: Path, home: Path, answer: Any
) -> None:
    script = fake_script(call("write_file", path="hello.txt", content="hi\n"), {"text": "OK."})
    worker, out = await started(root, ChatOptions(mode="ask", fake_script=script))
    worker.submit("Create hello.txt")
    request = await out.wait_for("request", kind="approval")
    worker.renderer.resolve(request["id"], answer)
    await out.wait_for("turn")
    assert not (root / "hello.txt").exists()
    await worker.close()


async def test_question_round_trip(root: Path, home: Path) -> None:
    question = {
        "text": "Which color?",
        "kind": "choice",
        "options": ["red", "blue"],
        "why": "style",
    }
    script = fake_script(call("ask_user", questions=[question]), {"text": "Blue it is."})
    worker, out = await started(root, ChatOptions(fake_script=script))
    worker.submit("Pick a color with me")
    request = await out.wait_for("request", kind="question")
    assert request["payload"]["questions"][0]["text"] == "Which color?"
    worker.renderer.resolve(request["id"], {"answers": [{"question_index": 0, "values": ["blue"]}]})
    await out.wait_for("turn")
    finished = [e for e in out.events() if e["kind"] == "tool_finished"]
    assert "blue" in finished[0]["result"]["text"]
    await worker.close()


async def test_cancel_stops_a_running_turn(root: Path, home: Path) -> None:
    script = fake_script({"text": "thinking...", "delay_s": 30}, {"text": "late"})
    worker, out = await started(root, ChatOptions(fake_script=script))
    worker.submit("Take your time")
    await out.wait_for("status", state="running")
    await asyncio.sleep(0.3)
    await asyncio.wait_for(worker.cancel(), 5)
    turn = await out.wait_for("turn")
    assert turn.get("cancelled") and not turn["ok"]
    assert worker.submit("again")  # the worker takes new turns after a cancel
    await worker.close()


async def test_busy_worker_refuses_a_second_turn(root: Path, home: Path) -> None:
    script = fake_script({"text": "slow", "delay_s": 30})
    worker, _out = await started(root, ChatOptions(fake_script=script))
    assert worker.submit("one")
    assert not worker.submit("two")
    await worker.close()


async def test_follow_up_carries_the_conversation(root: Path, home: Path) -> None:
    script = fake_script({"text": "Paris."}, {"text": "About 2 million."}, prompts=2)
    worker, out = await started(root, ChatOptions(fake_script=script))
    worker.submit("What is the capital of France?")
    await out.wait_for("turn")
    worker.submit("How many people live there?")
    await out.wait_for("turn")
    assert worker.ctx is not None
    fake = worker.ctx.cfg.instances["fake"]
    last_user = [m for m in fake.requests[-1].messages if m.role == "user"][-1].text()
    assert "Earlier in this conversation" in last_user
    assert "What is the capital of France?" in last_user and "Paris." in last_user
    assert "How many people live there?" in last_user
    await worker.close()


async def test_a_new_worker_picks_the_chat_up_again(root: Path, home: Path) -> None:
    script = fake_script({"text": "First answer."})
    worker, out = await started(root, ChatOptions(fake_script=script))
    worker.submit("first")
    await out.wait_for("turn")
    assert worker.ctx is not None
    session_id = worker.ctx.session.id
    await worker.close()
    again, _out = await started(root, ChatOptions(fake_script=fake_script({"text": "x"})))
    assert again.ctx is not None and again.ctx.session.id == session_id
    state = ChatState.load(home, "c1")
    assert state.session_id == session_id and [t.prompt for t in state.turns] == ["first"]
    await again.close()


async def test_slash_commands_answer_without_a_model(root: Path, home: Path) -> None:
    worker, out = await started(root, ChatOptions(fake_script=fake_script()))
    worker.submit("/help")
    result = await out.wait_for("command_result")
    assert "/undo" in result["text"]
    turn = await out.wait_for("turn")
    assert turn["ok"]
    assert ChatState.load(home, "c1").turns == []  # commands are not conversation turns
    await worker.close()


async def test_ready_lists_the_slash_commands(root: Path, home: Path) -> None:
    commands = root / ".forge" / "commands"
    commands.mkdir(parents=True)
    (commands / "review.md").write_text(
        "---\ndescription: Review the code\n---\nReview $ARGUMENTS\n"
    )
    out = Collector()
    worker = ChatWorker(root, "c1", ChatOptions(fake_script=fake_script()), out)  # type: ignore[arg-type]
    await worker.start()
    ready = await out.wait_for("ready")
    listed = {c["name"]: c for c in ready["commands"]}
    assert listed["/compact"]["usage"] == "/compact [hard]" and listed["/plan"]["help"]
    assert listed["/review"] == {"name": "/review", "usage": "/review [arguments]",
                                 "help": "Review the code", "custom": True}  # fmt: skip
    await worker.close()


def test_history_block_is_bounded() -> None:
    assert with_history("hi", []) == "hi"
    turns = [
        Turn(prompt=f"question {i} " + "x" * 5000, summary="y" * 9000, ok=True) for i in range(30)
    ]
    prompt = with_history("new message", turns)
    assert prompt.endswith("new message")
    assert "[Turn 30]" in prompt and "[Turn 1]" not in prompt
    assert len(prompt) < 20_000
