"""Secrets in tool output are masked before the model sees them."""

from typing import Any

import pytest

from forge.ctx import Ctx
from forge.providers.base import ToolCall, ToolResult
from forge.runtime.secrets import mask_secrets
from forge.tools import call_tool

SAMPLES = {
    "openai": "sk-proj-abcdefghijklmnopqrstuvwxyz0123456789ABCD",
    "anthropic": "sk-ant-api03-abcdefghijklmnopqrstuvwxyz0123456789-_abcdef",
    "github": "ghp_abcdefghijklmnopqrstuvwxyz0123456789",
    "aws": "AKIAIOSFODNN7EXAMPLE",
    "slack": "xoxb-123456789012-1234567890123-abcdefghijklmnopqrstuvwx",
    "google": "AIzaSyA-abcdefghijklmnopqrstuvwxyz12345",
}


@pytest.mark.parametrize("kind", sorted(SAMPLES))
def test_known_key_formats_are_masked(kind: str) -> None:
    text = f"config: token={SAMPLES[kind]} end"
    masked = mask_secrets(text, {})
    assert SAMPLES[kind] not in masked and "[masked secret]" in masked and masked.endswith(" end")


def test_private_keys_and_env_values_are_masked() -> None:
    pem = "-----BEGIN RSA PRIVATE KEY-----\nMIIEow\nabc\n-----END RSA PRIVATE KEY-----"
    assert "MIIEow" not in mask_secrets(f"key:\n{pem}\n", {})
    env = {"MY_SERVICE_TOKEN": "hunter2-very-secret", "PATH": "/usr/bin", "SHORT_KEY": "abc"}
    masked = mask_secrets("value hunter2-very-secret and /usr/bin and abc", env)
    assert "hunter2-very-secret" not in masked and "/usr/bin" in masked and " abc" in masked


def test_ordinary_text_is_untouched() -> None:
    text = "def add(a, b):\n    return a + b  # sk-short\nAKIA is a prefix"
    assert mask_secrets(text, {}) == text


async def run(ctx: Ctx, name: str, **arguments: Any) -> ToolResult:
    return await call_tool(ctx, ToolCall(id="c1", name=name, arguments=arguments))


async def test_tool_output_reaches_the_model_masked(
    ctx: Ctx, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DEPLOY_PASSWORD", "correct-horse-battery")
    (ctx.root / ".env").write_text(
        f"OPENAI_API_KEY={SAMPLES['openai']}\nDEPLOY=correct-horse-battery\n"
    )
    result = await run(ctx, "read_file", path=".env")
    assert (
        result.ok
        and SAMPLES["openai"] not in result.text
        and "correct-horse-battery" not in result.text
    )
    assert result.text.count("[masked secret]") == 2
