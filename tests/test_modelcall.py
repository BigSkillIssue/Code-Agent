"""One model turn through a role's fallback chain."""

from pathlib import Path

import pytest

from forge.config import ForgeConfig
from forge.modelcall import model_turn
from forge.providers.base import ProviderError, text_message
from forge.providers.fake import FakeProvider, FakeTurn
from forge.providers.registry import register_provider
from support import make_ctx


async def test_a_model_out_of_quota_for_hours_is_skipped(tmp_project: Path) -> None:
    """After a long rate limit (Gemini's daily quota) the session stops asking that model."""
    out = FakeTurn(error="rate_limit", retry_after_s=4 * 3600)
    fake = FakeProvider(
        roles={"first": [out, out, out], "second": [FakeTurn(text="a"), FakeTurn(text="b")]}
    )
    cfg = ForgeConfig(roles={"coder": ["fake/first", "fake/second"]})
    register_provider(cfg, fake)
    ctx = make_ctx(tmp_project, cfg=cfg)
    hello = [text_message("user", "hi")]
    first, _ = await model_turn(ctx, "coder", "", hello, [])
    second, _ = await model_turn(ctx, "coder", "", hello, [])
    assert (first.text(), second.text()) == ("a", "b")
    assert [r.model for r in fake.requests] == ["first", "second", "second"]


async def test_a_short_rate_limit_does_not_skip_the_model(tmp_project: Path) -> None:
    fake = FakeProvider(
        roles={
            "first": [FakeTurn(error="rate_limit", retry_after_s=5), FakeTurn(text="back")],
            "second": [FakeTurn(text="a")],
        }
    )
    cfg = ForgeConfig(roles={"coder": ["fake/first", "fake/second"]})
    register_provider(cfg, fake)
    ctx = make_ctx(tmp_project, cfg=cfg)
    hello = [text_message("user", "hi")]
    await model_turn(ctx, "coder", "", hello, [])
    again, _ = await model_turn(ctx, "coder", "", hello, [])
    assert again.text() == "back"


async def test_when_every_model_cools_down_they_are_still_tried(tmp_project: Path) -> None:
    out = FakeTurn(error="rate_limit", retry_after_s=4 * 3600)
    fake = FakeProvider(roles={"only": [out, FakeTurn(text="recovered")]})
    cfg = ForgeConfig(roles={"coder": ["fake/only"]})
    register_provider(cfg, fake)
    ctx = make_ctx(tmp_project, cfg=cfg)
    hello = [text_message("user", "hi")]
    with pytest.raises(ProviderError):
        await model_turn(ctx, "coder", "", hello, [])
    answer, _ = await model_turn(ctx, "coder", "", hello, [])
    assert answer.text() == "recovered"
