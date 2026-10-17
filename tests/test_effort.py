"""How hard reasoning models think (S74): the config's `effort`, `/effort` and `--effort`."""

from pathlib import Path

from forge.cli import build_parser, load
from forge.commands import run_command
from forge.config import ForgeConfig, load_config
from forge.modelcall import model_turn
from forge.providers.base import Capabilities, text_message
from forge.providers.fake import FakeProvider, FakeTurn
from forge.providers.registry import register_provider
from support import make_ctx

THINKER = Capabilities(context_window=128_000, max_output=8_192, reasoning=True)
HELLO = [text_message("user", "hi")]


def fake(caps: Capabilities = THINKER, answers: int = 1) -> FakeProvider:
    """A provider that answers `answers` times."""
    return FakeProvider([FakeTurn(text="ok")] * answers, caps=caps)


async def test_the_request_carries_the_configured_effort(tmp_project: Path) -> None:
    provider = fake()
    cfg = ForgeConfig(effort="high", roles={"coder": ["fake/any"]})
    register_provider(cfg, provider)
    await model_turn(make_ctx(tmp_project, cfg=cfg), "coder", "", HELLO, [])
    assert provider.requests[0].reasoning_effort == "high"


async def test_auto_and_models_that_do_not_reason_send_none(tmp_project: Path) -> None:
    thinker, plain = fake(), fake(Capabilities(reasoning=False))
    auto = ForgeConfig(roles={"coder": ["fake/any"]})
    register_provider(auto, thinker)
    await model_turn(make_ctx(tmp_project, cfg=auto), "coder", "", HELLO, [])
    high = ForgeConfig(effort="high", roles={"coder": ["fake/any"]})
    register_provider(high, plain)
    await model_turn(make_ctx(tmp_project, cfg=high), "coder", "", HELLO, [])
    assert auto.effort == "auto"
    assert thinker.requests[0].reasoning_effort is None
    assert plain.requests[0].reasoning_effort is None


async def test_slash_effort_overrides_the_config_for_the_session(tmp_project: Path) -> None:
    provider = fake(answers=2)
    cfg = ForgeConfig(effort="low", roles={"coder": ["fake/any"]})
    register_provider(cfg, provider)
    ctx = make_ctx(tmp_project, cfg=cfg)
    assert await run_command(ctx, "/effort") == "effort: low"
    assert await run_command(ctx, "/effort medium") == "effort: medium"
    await model_turn(ctx, "coder", "", HELLO, [])
    assert "unknown effort" in await run_command(ctx, "/effort extreme")
    assert await run_command(ctx, "/effort auto") == "effort: auto"
    await model_turn(ctx, "coder", "", HELLO, [])
    assert [r.reasoning_effort for r in provider.requests] == ["medium", None]
    assert cfg.effort == "low"  # the config itself is untouched
    assert "/effort [LEVEL]" in await run_command(ctx, "/help")


def test_the_effort_flag_and_an_untrusted_project_may_both_set_it(tmp_project: Path) -> None:
    (tmp_project / ".forge").mkdir(exist_ok=True)
    (tmp_project / ".forge" / "config.toml").write_text('effort = "medium"\n', encoding="utf-8")
    project = load_config(tmp_project)
    assert project.effort == "medium" and project.warnings == []  # not a safety setting
    options = build_parser().parse_args(["-C", str(tmp_project), "--effort", "high"])
    _, cfg = load(options)
    assert cfg.effort == "high"
    _, plain = load(build_parser().parse_args(["-C", str(tmp_project)]))
    assert plain.effort == "medium"
