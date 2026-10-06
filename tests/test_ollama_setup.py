"""`forge ollama setup` and `status`: hardware tiers, Ollama API calls, config written."""

import json
import tomllib
from pathlib import Path

import httpx
import pytest
import respx

from forge.config import ForgeConfig, ModelOverride
from forge.ollama_setup import (
    Hardware,
    context_warnings,
    create_variant,
    model_capabilities,
    pull,
    recommend,
    server_status,
    write_setup,
)

API = "http://localhost:11434"


@pytest.mark.parametrize(
    ("hardware", "model", "context"),
    [
        (Hardware(ram_gb=64, vram_gb=24), "qwen3-coder:30b", 32768),
        (Hardware(ram_gb=32, vram_gb=12), "qwen3:14b", 32768),
        (Hardware(ram_gb=16, vram_gb=8), "qwen3:8b", 32768),
        (Hardware(ram_gb=36, apple=True), "qwen3-coder:30b", 32768),
        (Hardware(ram_gb=24, apple=True), "qwen3:14b", 32768),
        (Hardware(ram_gb=16), "qwen3:4b-instruct", 16384),
        (Hardware(ram_gb=8), "qwen3:4b-instruct", 8192),
    ],
)
def test_the_suggestion_fits_the_hardware(hardware: Hardware, model: str, context: int) -> None:
    choice = recommend(hardware)
    assert (choice.model, choice.context) == (model, context)
    assert choice.reason


@respx.mock
async def test_status_reports_version_and_models() -> None:
    respx.get(f"{API}/api/version").mock(
        return_value=httpx.Response(200, json={"version": "0.40.0"})
    )
    respx.get(f"{API}/api/tags").mock(
        return_value=httpx.Response(
            200, json={"models": [{"name": "qwen3:4b"}, {"name": "forge-qwen3:4b"}]}
        )
    )
    status = await server_status(API)
    assert status is not None and status.version == "0.40.0"
    assert status.models == ["qwen3:4b", "forge-qwen3:4b"]


@respx.mock
async def test_status_is_none_when_ollama_is_not_running() -> None:
    respx.get(f"{API}/api/version").mock(side_effect=httpx.ConnectError("refused"))
    assert await server_status(API) is None


@respx.mock
async def test_pull_reports_progress() -> None:
    lines = [
        {"status": "pulling manifest"},
        {"status": "pulling abc", "total": 200, "completed": 100},
        {"status": "success"},
    ]
    route = respx.post(f"{API}/api/pull").mock(
        return_value=httpx.Response(200, text="\n".join(json.dumps(line) for line in lines))
    )
    seen: list[str] = []
    await pull(API, "qwen3:4b", seen.append)
    assert json.loads(route.calls[0].request.content) == {"model": "qwen3:4b", "stream": True}
    assert "pulling abc 50%" in seen and seen[-1] == "success"


@respx.mock
async def test_pull_errors_are_reported() -> None:
    respx.post(f"{API}/api/pull").mock(
        return_value=httpx.Response(200, text=json.dumps({"error": "file does not exist"}))
    )
    with pytest.raises(ValueError, match="file does not exist"):
        await pull(API, "nope:1b", lambda _: None)


@respx.mock
async def test_the_variant_gets_the_context_window() -> None:
    route = respx.post(f"{API}/api/create").mock(
        return_value=httpx.Response(200, json={"status": "success"})
    )
    name = await create_variant(API, "qwen3:4b", 16384)
    assert name == "forge-qwen3:4b"
    assert json.loads(route.calls[0].request.content) == {
        "model": "forge-qwen3:4b",
        "from": "qwen3:4b",
        "parameters": {"num_ctx": 16384},
        "stream": False,
    }


@respx.mock
async def test_capabilities_come_from_show() -> None:
    respx.post(f"{API}/api/show").mock(
        return_value=httpx.Response(200, json={"capabilities": ["completion", "tools"]})
    )
    assert await model_capabilities(API, "forge-qwen3:4b") == ["completion", "tools"]


def test_write_setup_sets_roles_and_model_and_keeps_the_rest(tmp_path: Path) -> None:
    path = tmp_path / "forge.toml"
    path.write_text(
        '# mine\n[roles]\napi-tester = ["openai/gpt-5-mini"]\n\n[web]\nsearch_backend = "brave"\n'
    )
    write_setup(path, "forge-qwen3:4b", 16384, tools=True)
    text = path.read_text()
    data = tomllib.loads(text)
    assert text.startswith("# mine\n")
    assert data["web"] == {"search_backend": "brave"}
    roles = data["roles"]
    assert roles["coder"] == ["ollama/forge-qwen3:4b"] and roles["planner"] == [
        "ollama/forge-qwen3:4b"
    ]
    assert roles["api-tester"] == ["openai/gpt-5-mini"]  # the user's own role stays
    assert "browser" not in roles  # needs a vision model; text models cannot drive it
    model = data["models"]["ollama/forge-qwen3:4b"]
    assert model == {
        "context_window": 16384,
        "tools": True,
        "vision": False,
        "cost_in": 0.0,
        "cost_out": 0.0,
    }


def test_ollama_models_without_a_context_entry_are_warned_about() -> None:
    cfg = ForgeConfig(roles={"coder": ["ollama/qwen3:8b"], "planner": ["anthropic/claude-sonnet"]})
    warnings = context_warnings(cfg)
    assert (
        len(warnings) == 1
        and "ollama/qwen3:8b" in warnings[0]
        and "forge ollama setup" in warnings[0]
    )
    cfg.models["ollama/qwen3:8b"] = ModelOverride(context_window=32768)
    assert context_warnings(cfg) == []


@respx.mock
def test_cli_setup_end_to_end(
    tmp_project: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from forge import ollama_cli
    from forge.cli import main

    monkeypatch.setenv("FORGE_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(ollama_cli, "detect_hardware", lambda: Hardware(ram_gb=16))
    respx.get(f"{API}/api/version").mock(
        return_value=httpx.Response(200, json={"version": "0.40.0"})
    )
    respx.get(f"{API}/api/tags").mock(return_value=httpx.Response(200, json={"models": []}))
    pulled = respx.post(f"{API}/api/pull").mock(
        return_value=httpx.Response(200, text=json.dumps({"status": "success"}))
    )
    respx.post(f"{API}/api/create").mock(
        return_value=httpx.Response(200, json={"status": "success"})
    )
    respx.post(f"{API}/api/show").mock(
        return_value=httpx.Response(200, json={"capabilities": ["completion", "tools"]})
    )
    assert main(["-C", str(tmp_project), "ollama", "setup", "--yes"]) == 0
    out = capsys.readouterr().out
    assert "suggestion: qwen3:4b-instruct" in out and "forge-qwen3:4b-instruct" in out
    assert json.loads(pulled.calls[0].request.content)["model"] == "qwen3:4b-instruct"
    data = tomllib.loads((tmp_path / "home" / "forge.toml").read_text())
    assert data["roles"]["coder"] == ["ollama/forge-qwen3:4b-instruct"]
    assert data["models"]["ollama/forge-qwen3:4b-instruct"]["context_window"] == 16384


@respx.mock
def test_cli_explains_how_to_start_ollama(
    tmp_project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from forge.cli import main

    respx.get(f"{API}/api/version").mock(side_effect=httpx.ConnectError("refused"))
    assert main(["-C", str(tmp_project), "ollama", "status"]) == 1
    assert "ollama.com/download" in capsys.readouterr().out
