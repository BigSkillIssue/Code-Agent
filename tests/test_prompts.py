"""Tests for prompts.py: every prompt renders, slots are checked, overrides apply."""

import re

import pytest

from forge import prompts

FULL = {slot: f"<{slot}>" for slot in prompts.KNOWN_SLOTS}


@pytest.mark.parametrize("name", sorted(prompts.PROMPTS))
def test_every_prompt_renders_with_a_full_context(name: str) -> None:
    text = prompts.render(name, **FULL)
    static, tail = prompts.PROMPTS[name]
    assert text.startswith(static)
    assert text.endswith(tail.format(**FULL))  # volatile slots always come last


@pytest.mark.parametrize("name", sorted(prompts.PROMPTS))
def test_static_text_has_no_slots(name: str) -> None:
    static, tail = prompts.PROMPTS[name]
    assert not any(f"{{{slot}}}" in static for slot in prompts.KNOWN_SLOTS)
    assert prompts.slots_of(tail) <= prompts.KNOWN_SLOTS


def test_missing_slot_raises() -> None:
    with pytest.raises(ValueError, match=r"needs slots: .*cwd"):
        prompts.render("coder", os="Linux")


def test_unknown_slot_raises() -> None:
    with pytest.raises(ValueError, match="unknown prompt slots: cwdd"):
        prompts.render("coder", cwdd="/x", **FULL)


def test_unknown_prompt_raises() -> None:
    with pytest.raises(KeyError):
        prompts.render("nope", **FULL)


def test_role_prompts_share_the_common_text() -> None:
    assert prompts.SAFETY in prompts.render("refiner", **FULL)
    assert prompts.SAFETY in prompts.render("planner", **FULL)
    assert prompts.TOOL_RULES in prompts.render("coder", **FULL)
    assert prompts.BASE in prompts.render("coder", **FULL)


def test_overrides_follow_the_model_family() -> None:
    gemini = prompts.render("coder", model="gemini-2.5-pro", **FULL)
    plain = prompts.render("coder", model="gpt-5", **FULL)
    local = prompts.render("coder", model="qwen3:32b", **FULL)
    assert prompts.OVERRIDES["gemini"]["coder"] in gemini
    assert prompts.OVERRIDES["gemini"]["coder"] not in plain
    assert prompts.OVERRIDES["local"]["coder"] in local
    assert prompts.model_family("claude-sonnet-5-5") == ""


def test_version_is_set() -> None:
    assert re.fullmatch(r"\d{4}\.\d{2}\.\d+", prompts.PROMPTS_VERSION)
