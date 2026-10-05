"""Live provider calls (marked `live`, skipped by default; need API keys or a local server)."""

import os

import httpx
import pytest

from forge.config import ProviderConfig
from forge.providers.base import ChatRequest, text_message
from forge.providers.openai_compat import OpenAICompatProvider

pytestmark = pytest.mark.live


async def say_hi(provider: OpenAICompatProvider, model: str) -> str:
    req = ChatRequest(
        model=model, system="Answer in one word.", messages=[text_message("user", "Say hi")]
    )
    items = [item async for item in provider.stream(req)]
    assert items[-1].done is not None
    assert items[-1].usage is not None
    return items[-1].done.text()


@pytest.mark.skipif(not os.environ.get("OPENAI_API_KEY"), reason="OPENAI_API_KEY not set")
async def test_live_openai() -> None:
    cfg = ProviderConfig(base_url="https://api.openai.com/v1", api_key_env="OPENAI_API_KEY")
    text = await say_hi(
        OpenAICompatProvider("openai", cfg), os.environ.get("OPENAI_LIVE_MODEL", "gpt-5-mini")
    )
    assert text.strip()


def ollama_running() -> bool:
    try:
        return httpx.get("http://localhost:11434/v1/models", timeout=1).status_code == 200
    except httpx.HTTPError:
        return False


@pytest.mark.skipif(not ollama_running(), reason="no Ollama server on localhost:11434")
async def test_live_ollama() -> None:
    models = httpx.get("http://localhost:11434/v1/models", timeout=5).json()["data"]
    cfg = ProviderConfig(base_url="http://localhost:11434/v1")
    text = await say_hi(OpenAICompatProvider("ollama", cfg), models[0]["id"])
    assert text.strip()
