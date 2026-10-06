"""Early tool start: safe calls run while the model is still writing (S52)."""

from pathlib import Path

from forge.agent import run_agent
from forge.config import ForgeConfig
from forge.ctx import Ctx
from forge.events import Event, ModelDone, ToolFinished, ToolStarted
from forge.providers.fake import FakeProvider, FakeToolCall, FakeTurn
from forge.providers.registry import register_provider
from forge.tools import REGISTRY, can_run_concurrently
from support import drain, make_ctx


def agent_ctx(root: Path, fake: FakeProvider, chain: list[str] | None = None) -> Ctx:
    cfg = ForgeConfig(roles={"coder": chain or ["fake/coder"]})
    register_provider(cfg, fake)
    return make_ctx(root, cfg=cfg)


def calls(
    *specs: tuple[str, dict[str, object]], delay_s: float = 0.3, error: str | None = None
) -> FakeTurn:
    return FakeTurn(
        tool_calls=[FakeToolCall(name=name, arguments=args) for name, args in specs],
        delay_s=delay_s,
        error=error,  # type: ignore[arg-type]
    )


def order(events: list[Event]) -> list[str]:
    names = []
    for event in events:
        if isinstance(event, ToolStarted):
            names.append(f"start:{event.call.name}")
        elif isinstance(event, ModelDone):
            names.append("done")
    return names


async def test_a_read_starts_before_the_reply_ends(tmp_project: Path) -> None:
    (tmp_project / "a.txt").write_text("hello\n")
    fake = FakeProvider([calls(("read_file", {"path": "a.txt"})), FakeTurn(text="ok")])
    ctx = agent_ctx(tmp_project, fake)
    events = ctx.bus.subscribe("*")
    result = await run_agent(ctx, "read a.txt")
    seen = await drain(events)
    assert order(seen)[:2] == ["start:read_file", "done"]
    assert sum(isinstance(e, ToolFinished) for e in seen) == 1  # run once, not again
    tool_result = result.messages[2].tool_result
    assert tool_result is not None and "hello" in tool_result.text


async def test_writes_and_everything_after_them_wait(tmp_project: Path) -> None:
    fake = FakeProvider(
        [
            calls(
                ("write_file", {"path": "b.txt", "content": "new\n"}),
                ("read_file", {"path": "b.txt"}),
            ),
            FakeTurn(text="ok"),
        ]
    )
    ctx = agent_ctx(tmp_project, fake)
    events = ctx.bus.subscribe("*")
    result = await run_agent(ctx, "write then read")
    assert order(await drain(events))[:3] == ["done", "start:write_file", "start:read_file"]
    read = result.messages[3].tool_result
    assert read is not None and "new" in read.text  # the read saw the write


async def test_a_failed_stream_discards_early_calls(tmp_project: Path) -> None:
    fake = FakeProvider(
        roles={
            "first": [calls(("list_dir", {"path": "."}), error="overloaded")],
            "second": [FakeTurn(text="answered by the fallback")],
        }
    )
    ctx = agent_ctx(tmp_project, fake, chain=["fake/first", "fake/second"])
    result = await run_agent(ctx, "look around")
    assert result.text == "answered by the fallback"
    assert not any(m.tool_result for m in result.messages)


def test_browser_tools_never_run_concurrently() -> None:
    assert can_run_concurrently(REGISTRY["read_file"])
    assert not can_run_concurrently(REGISTRY["browser_open"])
    assert not can_run_concurrently(REGISTRY["write_file"])
