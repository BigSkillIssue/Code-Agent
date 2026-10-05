"""Tests for the agent loop with a scripted FakeProvider."""

import asyncio
from pathlib import Path
from typing import Annotated, Any

import pytest

from forge import tools
from forge.agent import run_agent
from forge.config import ForgeConfig
from forge.ctx import Ctx
from forge.events import ErrorEvent, ModelDelta, ModelDone, ToolFinished, ToolStarted
from forge.providers.base import ChatRequest, Message
from forge.providers.fake import FakeProvider, FakeToolCall, FakeTurn
from forge.providers.registry import register_provider
from support import drain, make_ctx


def scripted(tmp_project: Path, *providers: FakeProvider, chain: list[str] | None = None) -> Ctx:
    cfg = ForgeConfig(roles={"coder": chain or [f"{p.name}/model" for p in providers]})
    for provider in providers:
        register_provider(cfg, provider)
    return make_ctx(tmp_project, cfg=cfg)


def call(name: str, **arguments: Any) -> FakeToolCall:
    return FakeToolCall(name=name, arguments=arguments)


async def test_reads_then_edits_and_stops(tmp_project: Path) -> None:
    (tmp_project / "app.py").write_text("print('helo')\n")
    fake = FakeProvider(
        [
            FakeTurn(text="Reading the file.", tool_calls=[call("read_file", path="app.py")]),
            FakeTurn(tool_calls=[call("edit_file", path="app.py", old="helo", new="hello")]),
            FakeTurn(text="Fixed the typo."),
        ]
    )
    ctx = scripted(tmp_project, fake)
    events = ctx.bus.subscribe("*")
    result = await run_agent(ctx, "fix the typo in app.py")
    assert result.stopped == "done"
    assert result.text == "Fixed the typo."
    assert (tmp_project / "app.py").read_text() == "print('hello')\n"
    roles = [m.role for m in result.messages]
    assert roles == ["user", "assistant", "tool", "assistant", "tool", "assistant"]
    kinds = [type(e) for e in await drain(events)]
    assert kinds[:2] == [ModelDelta, ModelDelta]  # "Reading the file." arrives in chunks
    assert ModelDone in kinds and ToolStarted in kinds and ToolFinished in kinds
    assert kinds.index(ToolStarted) < kinds.index(ToolFinished)
    assert result.usage.input_tokens > 0


async def test_system_prompt_and_tools_are_sent(tmp_project: Path) -> None:
    fake = FakeProvider([FakeTurn(text="hi")])
    ctx = scripted(tmp_project, fake)
    await run_agent(ctx, "say hi")
    request = fake.requests[0]
    assert "You are Forge" in request.system
    assert str(ctx.cwd) in request.system
    assert {"read_file", "edit_file", "bash"} <= {t.name for t in request.tools}
    assert request.messages[-1].text() == "say hi"


async def test_stops_at_max_turns(tmp_project: Path) -> None:
    (tmp_project / "a.txt").write_text("x\n")

    def again(req: ChatRequest) -> FakeTurn:
        return FakeTurn(tool_calls=[call("read_file", path="a.txt")])

    fake = FakeProvider([again] * 10)
    result = await run_agent(scripted(tmp_project, fake), "loop forever", max_turns=3)
    assert result.stopped == "max_turns"
    assert len(fake.requests) == 3


async def test_failing_tool_result_reaches_next_turn(tmp_project: Path) -> None:
    fake = FakeProvider(
        [
            FakeTurn(tool_calls=[call("read_file", path="missing.py")]),
            FakeTurn(text="It does not exist."),
        ]
    )
    result = await run_agent(scripted(tmp_project, fake), "read missing.py")
    assert result.stopped == "done"
    seen: Message = fake.requests[1].messages[-1]
    assert seen.role == "tool" and seen.tool_result is not None
    assert not seen.tool_result.ok and seen.tool_result.code == "not_found"


async def test_fallback_to_next_model_on_provider_error(tmp_project: Path) -> None:
    broken = FakeProvider([FakeTurn(error="overloaded")], name="first")
    working = FakeProvider([FakeTurn(text="from the second model")], name="second")
    ctx = scripted(tmp_project, broken, working)
    events = ctx.bus.subscribe("*")
    result = await run_agent(ctx, "hello")
    assert result.text == "from the second model"
    errors = [e for e in await drain(events) if isinstance(e, ErrorEvent)]
    assert errors and "first/model" in errors[0].message


async def test_all_models_failing_stops_with_error(tmp_project: Path) -> None:
    fake = FakeProvider([FakeTurn(error="auth")])
    result = await run_agent(scripted(tmp_project, fake), "hello")
    assert result.stopped == "error"


async def test_transcript_is_appended_to_the_session(tmp_project: Path) -> None:
    fake = FakeProvider([FakeTurn(text="ok")])
    ctx = scripted(tmp_project, fake)
    await run_agent(ctx, "first task")
    assert [m.role for m in ctx.session.messages] == ["user", "assistant"]


@pytest.fixture
def probes(monkeypatch: pytest.MonkeyPatch) -> dict[str, int]:
    """Two slow test tools that record how many calls ran at the same time."""
    state = {"running": 0, "peak": 0}

    async def probe(ctx: Ctx, n: Annotated[int, "Which call."]) -> str:
        """Probe concurrency."""
        state["running"] += 1
        state["peak"] = max(state["peak"], state["running"])
        await asyncio.sleep(0.05)
        state["running"] -= 1
        return f"probe {n}"

    async def poke(ctx: Ctx, n: Annotated[int, "Which call."]) -> str:
        """Probe concurrency, but with side effects."""
        return await probe(ctx, n)

    monkeypatch.setitem(
        tools.REGISTRY, "probe", tools.make_tool_def(probe, group="search", read_only=True)
    )
    monkeypatch.setitem(tools.REGISTRY, "poke", tools.make_tool_def(poke, group="files"))
    return state


@pytest.mark.parametrize(("name", "peak"), [("probe", 3), ("poke", 1)])
async def test_parallel_only_for_read_only_calls(
    tmp_project: Path, probes: dict[str, int], name: str, peak: int
) -> None:
    fake = FakeProvider(
        [FakeTurn(tool_calls=[call(name, n=i) for i in range(3)]), FakeTurn(text="done")]
    )
    result = await run_agent(scripted(tmp_project, fake), "probe")
    assert probes["peak"] == peak
    tool_texts = [m.tool_result.text for m in result.messages if m.tool_result]
    assert tool_texts == ["probe 0", "probe 1", "probe 2"]  # results keep the call order
