"""The Python API: Forge(...).run() and .stream(), with injected ports."""

import json
from pathlib import Path

from forge import Forge
from forge.config import ForgeConfig
from forge.events import Event, ModelDelta, SessionDone
from forge.local.memory_store import MemoryStore
from forge.plan import Question, TaskSpec
from forge.ports import Answer, Approval, Session
from forge.providers.base import ToolCall
from forge.providers.fake import FakeProvider, FakeToolCall, FakeTurn
from forge.providers.registry import register_provider
from forge.wiring import BUILTIN_FAKE, install_fake
from support import NoExecutor


class AnsweringRenderer:
    """A program's own renderer: picks SQLite, approves edits, keeps the events."""

    def __init__(self) -> None:
        self.events: list[Event] = []
        self.questions: list[str] = []

    async def show(self, event: Event) -> None:
        self.events.append(event)

    async def ask(self, questions: list[Question]) -> list[Answer]:
        self.questions += [q.text for q in questions]
        return [Answer(question_index=i, values=["SQLite"]) for i in range(len(questions))]

    async def approve(self, call: ToolCall, reason: str) -> Approval:
        return Approval(allow=True)


class RecordingStore(MemoryStore):
    """A store that counts what it is asked to save."""

    def __init__(self) -> None:
        super().__init__()
        self.saved: list[str] = []

    async def save_session(self, s: Session) -> None:
        self.saved.append(s.status)
        await super().save_session(s)


def cache_task_config() -> tuple[ForgeConfig, FakeProvider]:
    question = Question(
        text="Which database?",
        kind="choice",
        options=["Redis", "SQLite"],
        default="Redis",
        why="dep",
    )
    spec = TaskSpec(
        goal="Add a cache",
        context="",
        requirements=["r"],
        acceptance_criteria=["cache exists"],
        open_questions=[question],
        size="medium",
    )
    merged = spec.model_copy(update={"open_questions": [], "requirements": ["use SQLite"]})
    steps = [{"title": "Add cache", "detail": "d", "check": "review: cache exists"}]
    plan = FakeTurn(tool_calls=[FakeToolCall(name="submit_plan", arguments={"steps": steps})])
    write = FakeToolCall(name="write_file", arguments={"path": "cache.py", "content": "c = {}\n"})
    verdict = FakeTurn(text=json.dumps({"ok": True, "summary": "Cache added."}))
    fake = FakeProvider(
        roles={
            "refiner": [
                FakeTurn(text=spec.model_dump_json()),
                FakeTurn(text=merged.model_dump_json()),
            ],
            "planner": [plan, FakeTurn(text="ok")],
            "coder": [FakeTurn(tool_calls=[write]), FakeTurn(text="written")],
            "reviewer": [FakeTurn(text='{"pass": true, "reason": "ok"}'), verdict],
        }
    )
    cfg = ForgeConfig(roles={r: [f"fake/{r}"] for r in ("refiner", "planner", "coder", "reviewer")})
    register_provider(cfg, fake)
    return cfg, fake


async def test_custom_renderer_answers_and_custom_store_receives_saves(tmp_project: Path) -> None:
    renderer, store = AnsweringRenderer(), RecordingStore()
    cfg, fake = cache_task_config()
    forge = Forge(cfg, renderer=renderer, store=store, executor=NoExecutor(), root=tmp_project)
    report = await forge.run("add a cache")
    assert report.ok and report.summary == "Cache added."
    assert renderer.questions == ["Which database?"]
    merge = [r for r in fake.requests if r.model == "refiner"][1]
    assert "- Which database? -> SQLite" in merge.messages[-1].text()
    assert (tmp_project / "cache.py").read_text() == "c = {}\n"
    assert store.saved and store.saved[-1] == "done"
    assert isinstance(renderer.events[-1], SessionDone) and renderer.events[-1].ok


async def test_stream_yields_events_until_session_done(tmp_project: Path) -> None:
    cfg = ForgeConfig()
    install_fake(cfg, FakeProvider.from_data(BUILTIN_FAKE))
    forge = Forge(cfg, store=MemoryStore(), executor=NoExecutor(), root=tmp_project)
    seen = [event async for event in forge.stream("say hello")]
    assert any(isinstance(e, ModelDelta) for e in seen)
    assert isinstance(seen[-1], SessionDone) and seen[-1].ok
    assert "Hello from the fake provider." in seen[-1].report


async def test_headless_forge_without_approval_refuses(tmp_project: Path) -> None:
    cfg = ForgeConfig()
    script = {"roles": BUILTIN_FAKE["roles"], "turns": [
        {"tool_calls": [{"name": "web_fetch", "arguments": {"url": "https://example.com"}}]},
        {"text": "could not fetch"},
    ]}  # fmt: skip
    fake = FakeProvider.from_data(script)
    install_fake(cfg, fake)
    forge = Forge(cfg, store=MemoryStore(), executor=NoExecutor(), root=tmp_project, approve=False)
    await forge.run("fetch it")
    refused = fake.requests[-1].messages[-1].tool_result
    assert refused is not None and refused.code == "permission_denied"
    assert "approvals are off for this run" in refused.text
