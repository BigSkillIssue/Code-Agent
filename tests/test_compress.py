"""Context compression: trim, summarize, reset (docs/PLAN.md: Context compression)."""

from pathlib import Path

from forge.agent import run_agent
from forge.commands import run_command
from forge.compress import COMPACTED_TAG, compact, request_tokens, trim
from forge.config import ForgeConfig
from forge.ctx import Ctx
from forge.events import Compacted
from forge.plan import Plan, Step, TaskSpec, checklist
from forge.providers.base import (
    Capabilities,
    Message,
    TextPart,
    ToolCall,
    ToolResult,
    text_message,
)
from forge.providers.fake import FakeProvider, FakeToolCall, FakeTurn
from forge.providers.registry import register_provider
from support import drain, make_ctx

WINDOW = 8_000
SUMMARY = "## Goal\nfix the parser\n## Decisions\nkeep the API\n## Next\nrun the tests"


def small_window_ctx(
    root: Path, *turns: FakeTurn, window: int = WINDOW
) -> tuple[Ctx, FakeProvider]:
    caps = Capabilities(context_window=window, max_output=1_000)
    fake = FakeProvider(
        list(turns), roles={"compressor": [FakeTurn(text=SUMMARY)] * 300}, caps=caps
    )
    cfg = ForgeConfig(roles={"coder": ["fake/coder"], "compressor": ["fake/compressor"]})
    register_provider(cfg, fake)
    ctx = make_ctx(root, cfg=cfg)
    spec = TaskSpec(
        goal="fix the parser",
        context="",
        requirements=[],
        acceptance_criteria=["tests pass"],
        size="small",
    )
    ctx.session.spec = spec
    ctx.session.plan = Plan(
        spec=spec,
        steps=[
            Step(id="s1", title="Find the bug", detail="", check="review: found", status="done"),
            Step(id="s2", title="Fix the parser", detail="", check="pytest", status="doing"),
        ],
    )
    return ctx, fake


def turn(i: int) -> list[Message]:
    """One assistant tool call and its result; every 25th call fails, the next one succeeds."""
    name = "bash" if i % 25 in (0, 1) else "read_file"
    args = {"command": "pytest"} if name == "bash" else {"path": f"src/m{i % 7}.py"}
    call = ToolCall(id=f"call_{i}", name=name, arguments=args)
    failed = i % 25 == 0
    text = (
        f"error[exit_nonzero]: E{i} AssertionError in test_parse" if failed else f"line {i}\n" * 200
    )
    return [
        Message(role="assistant", parts=[TextPart(text=f"step {i}")], tool_calls=[call]),
        Message(role="tool", tool_result=ToolResult(call_id=call.id, ok=not failed, text=text)),
    ]


async def test_200_turns_stay_inside_an_8k_window(tmp_project: Path) -> None:
    ctx, _ = small_window_ctx(tmp_project)
    events = ctx.bus.subscribe("*")
    messages = [text_message("user", "fix the parser")]
    for i in range(200):
        messages = await compact(
            ctx, messages, role="coder", system="sys", tools=[], task="fix the parser"
        )
        assert request_tokens("sys", messages, []) <= WINDOW
        if messages[0].text().startswith(COMPACTED_TAG):
            first = messages[0].text()
            assert ctx.session.plan is not None
            assert checklist(ctx.session.plan) in first  # the plan survives
        messages += turn(i)
        if i == 175:
            failure = messages[-1]
            assert failure.tool_result is not None and not failure.tool_result.ok
        if i >= 176:
            # E175 is unresolved: no bash call has succeeded since; it must survive compaction
            context = "\n".join(
                m.text() or (m.tool_result.text if m.tool_result else "") for m in messages
            )
            assert "E175" in context
    compacted = [e for e in await drain(events) if isinstance(e, Compacted)]
    assert {e.level for e in compacted} & {2, 3}
    assert all(e.tokens_after < e.tokens_before for e in compacted)


async def test_plan_spec_and_errors_survive_a_reset(tmp_project: Path) -> None:
    ctx, _ = small_window_ctx(tmp_project)
    messages = [text_message("user", "fix the parser"), *turn(0), *turn(2)]
    assert await run_command(ctx, "/compact hard")
    result = await compact(ctx, messages, role="coder", system="s", tools=[], task="fix the parser")
    assert len(result) == 1
    text = result[0].text()
    for part in (
        "<plan>",
        "Fix the parser",
        "<spec>",
        "<summary>\n## Goal",
        "E0 AssertionError",
        "<task>\nfix the parser",
    ):
        assert part in text
    assert ctx.session.summary.startswith("## Goal")


async def test_level_2_keeps_recent_turns(tmp_project: Path) -> None:
    ctx, _ = small_window_ctx(tmp_project, window=200_000)
    messages = [text_message("user", "go")] + [m for i in range(2, 12) for m in turn(i)]
    await run_command(ctx, "/compact")
    result = await compact(ctx, messages, role="coder", system="s", tools=[], task="go")
    assert result[0].text().startswith(COMPACTED_TAG)
    assert result[1].role == "assistant"
    assert result[-1] == messages[-1]


def test_trim_cuts_old_outputs_and_stubs_superseded_reads() -> None:
    messages = [m for i in range(2, 12) for m in turn(i)]  # m{i%7}: m2..m6, m0, m1, m2..m4
    trimmed = trim(messages)
    first = trimmed[1].tool_result
    assert first is not None and first.text == "[superseded: src/m2.py was read again later]"
    oldest_unique = trimmed[5].tool_result  # src/m4.py read again later too
    assert oldest_unique is not None and "superseded" in oldest_unique.text
    assert trimmed[-1] == messages[-1]  # recent results are never trimmed
    assert messages[1].tool_result is not None and messages[1].tool_result.text.startswith("line")


def test_trim_cuts_long_old_output_to_head_and_tail() -> None:
    calls = [ToolCall(id=f"b{i}", name="bash", arguments={"command": "ls"}) for i in range(8)]
    messages: list[Message] = []
    for call in calls:
        messages.append(Message(role="assistant", tool_calls=[call]))
        messages.append(
            Message(role="tool", tool_result=ToolResult(call_id=call.id, ok=True, text="x" * 5000))
        )
    result = trim(messages)[1].tool_result
    assert result is not None and "chars trimmed" in result.text and len(result.text) < 2000


async def test_agent_compacts_but_store_keeps_full_transcript(tmp_project: Path) -> None:
    for n in range(7):
        (tmp_project / f"f{n}.txt").write_text(f"content {n}\n" * 300)
    turns = [
        FakeTurn(tool_calls=[FakeToolCall(name="read_file", arguments={"path": f"f{i % 7}.txt"})])
        for i in range(30)
    ]
    ctx, _ = small_window_ctx(tmp_project, *turns, FakeTurn(text="done"), window=6_000)
    events = ctx.bus.subscribe("*")
    result = await run_agent(ctx, "read everything")
    assert result.stopped == "done"
    assert len(ctx.session.messages) == 1 + 30 * 2 + 1
    assert all(
        m.tool_result is None or m.tool_result.text.startswith("file:")
        for m in ctx.session.messages
    )
    assert any(isinstance(e, Compacted) for e in await drain(events))


async def test_context_command_reports_usage(tmp_project: Path) -> None:
    ctx, _ = small_window_ctx(tmp_project)
    assert await run_command(ctx, "/context") == "no model request yet"
    await compact(
        ctx, [text_message("user", "hi")], role="coder", system="system text", tools=[], task="hi"
    )
    report = await run_command(ctx, "/context")
    assert report.startswith("context: ") and "of 7,000 tokens" in report and "user" in report
