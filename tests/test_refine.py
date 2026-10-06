"""Tests for the refine stage: context gathering and TaskSpec parsing."""

import json
from pathlib import Path

import pytest

from forge.config import ForgeConfig
from forge.ctx import Ctx
from forge.pipeline import PipelineError, refine
from forge.providers.fake import FakeProvider, FakeTurn
from forge.providers.registry import register_provider
from support import make_ctx

CASES = json.loads((Path(__file__).parent / "fixtures" / "refine" / "cases.json").read_text())


def refiner_ctx(root: Path, *turns: FakeTurn) -> tuple[Ctx, FakeProvider]:
    fake = FakeProvider(roles={"refiner": list(turns)})
    cfg = ForgeConfig(roles={"refiner": ["fake/refiner"], "coder": ["fake/coder"]})
    register_provider(cfg, fake)
    return make_ctx(root, cfg=cfg), fake


@pytest.mark.parametrize("case", CASES, ids=[c["prompt"][:30] for c in CASES])
async def test_recorded_prompts_give_valid_specs(tmp_project: Path, case: dict[str, str]) -> None:
    ctx, fake = refiner_ctx(tmp_project, FakeTurn(text=case["response"]))
    spec = await refine(case["prompt"], ctx)
    assert spec.size == case["size"]
    assert spec.acceptance_criteria
    assert fake.requests[0].messages[0].text() == case["prompt"]
    assert fake.requests[0].json_schema is not None


async def test_invalid_json_then_valid_json(tmp_project: Path) -> None:
    good = CASES[1]["response"]
    ctx, fake = refiner_ctx(
        tmp_project, FakeTurn(text="I think we should add a flag."), FakeTurn(text=good)
    )
    spec = await refine("Add a --verbose flag", ctx)
    assert spec.size == "small"
    retry = fake.requests[1].messages[-1].text()
    assert "could not be used" in retry and "no JSON object" in retry


async def test_two_invalid_answers_fail(tmp_project: Path) -> None:
    ctx, _ = refiner_ctx(tmp_project, FakeTurn(text="nope"), FakeTurn(text='{"goal": 1}'))
    with pytest.raises(PipelineError, match="no valid task specification"):
        await refine("anything", ctx)


async def test_typo_fix_is_trivial(tmp_project: Path) -> None:
    ctx, _ = refiner_ctx(tmp_project, FakeTurn(text=CASES[0]["response"]))
    assert (await refine(CASES[0]["prompt"], ctx)).size == "trivial"


async def test_context_reaches_the_refiner(tmp_project: Path) -> None:
    (tmp_project / "src").mkdir()
    (tmp_project / "src" / "app.py").write_text("x = 1\n")
    (tmp_project / "FORGE.md").write_text("Run tests with `make test`.")
    ctx, fake = refiner_ctx(tmp_project, FakeTurn(text=CASES[1]["response"]))
    await refine("Add a flag", ctx)
    system = fake.requests[0].system
    assert "app.py" in system and "Git status:" in system and "make test" in system


def test_refiner_does_not_ask_for_what_the_agents_can_read() -> None:
    """A live run asked the user for test_calc.py's contents; the agents read files themselves."""
    from forge import prompts

    assert "Never ask for file contents" in prompts.PROMPTS["refiner"][0]
