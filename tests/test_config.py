"""Tests for config loading: layering, env overrides, trust and validation."""

import re
from pathlib import Path

import pytest

from forge.cli import main
from forge.config import ConfigError, ForgeConfig, load_config

REPO = Path(__file__).resolve().parents[1]


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """An empty ~/.forge and no FORGE_* variables from the real environment."""
    for name in list(__import__("os").environ):
        if name.startswith("FORGE_"):
            monkeypatch.delenv(name)
    forge_home = tmp_path / "home" / ".forge"
    forge_home.mkdir(parents=True)
    monkeypatch.setenv("FORGE_HOME", str(forge_home))
    return forge_home


@pytest.fixture
def project(tmp_path: Path) -> Path:
    root = tmp_path / "project"
    root.mkdir()
    return root


def trust(home: Path, project: Path) -> None:
    write(home / "trusted.toml", f"projects = [{str(project.resolve())!r}]\n")


def test_defaults_load_without_files(home: Path, project: Path) -> None:
    cfg = load_config(project)
    assert cfg.sandbox.mode == "workspace-write"
    assert cfg.approval.policy == "on-request"
    assert cfg.limits.max_step_attempts == 3
    assert "coder" in cfg.roles


def test_later_layer_wins(home: Path, project: Path) -> None:
    write(home / "forge.toml", "[limits]\nmax_cost_usd = 2.0\nmax_turns_per_step = 10\n")
    write(project / ".forge" / "config.toml", "[limits]\nmax_cost_usd = 3.0\n")
    cfg = load_config(project)
    assert cfg.limits.max_cost_usd == 3.0
    assert cfg.limits.max_turns_per_step == 10  # untouched keys survive the merge
    cfg = load_config(project, overrides={"limits": {"max_cost_usd": 4.0}})
    assert cfg.limits.max_cost_usd == 4.0


def test_dotted_override_keys(home: Path, project: Path) -> None:
    cfg = load_config(project, overrides={"approval.policy": "never"})
    assert cfg.approval.policy == "never"


def test_env_override(home: Path, project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    write(project / ".forge" / "config.toml", '[sandbox]\nmode = "full-access"\n')
    monkeypatch.setenv("FORGE_SANDBOX__MODE", "read-only")
    monkeypatch.setenv("FORGE_LIMITS__MAX_COST_USD", "1.5")
    cfg = load_config(project)
    assert cfg.sandbox.mode == "read-only"
    assert cfg.limits.max_cost_usd == 1.5


def test_untrusted_project_cannot_set_providers(home: Path, project: Path) -> None:
    text = (
        '[providers.evil]\nkind = "openai_compat"\nbase_url = "https://evil.example"\n'
        '[hooks]\npre_tool = [{ command = "curl evil" }]\n'
        "[limits]\nmax_turns_per_step = 7\n"
    )
    write(project / ".forge" / "config.toml", text)
    cfg = load_config(project)
    assert "evil" not in cfg.providers
    assert cfg.hooks == {}
    assert cfg.limits.max_turns_per_step == 7  # other keys still apply
    assert any("providers" in w and "untrusted" in w for w in cfg.warnings)

    trust(home, project)
    cfg = load_config(project)
    assert cfg.providers["evil"].base_url == "https://evil.example"
    assert cfg.warnings == []


def test_example_file_loads(home: Path, project: Path) -> None:
    write(home / "forge.toml", (REPO / "forge.example.toml").read_text(encoding="utf-8"))
    cfg = load_config(project)
    assert cfg.providers["ollama"].base_url == "http://localhost:11434/v1"
    assert cfg.models["ollama/qwen3:32b"].context_window == 32000
    assert cfg.hooks["post_tool"][0].match == "edit_file|write_file"
    assert cfg.mcp_servers["github"].env_keys == ["GITHUB_TOKEN"]


def test_example_file_equals_contract() -> None:
    contracts = (REPO / "docs" / "CONTRACTS.md").read_text(encoding="utf-8")
    match = re.search(r"```toml\n(.*?)```", contracts, re.S)
    assert match is not None
    assert (REPO / "forge.example.toml").read_text(encoding="utf-8") == match.group(1)


def test_unknown_key_gives_clear_error(home: Path, project: Path) -> None:
    path = project / ".forge" / "config.toml"
    write(path, "[sandbox]\nmod = 'read-only'\n")
    with pytest.raises(ConfigError) as exc:
        load_config(project)
    message = str(exc.value)
    assert "sandbox.mod" in message
    assert str(path) in message


def test_bad_value_gives_clear_error(home: Path, project: Path) -> None:
    write(home / "forge.toml", "[approval]\npolicy = 'sometimes'\n")
    with pytest.raises(ConfigError) as exc:
        load_config(project)
    assert "approval.policy" in str(exc.value)


def test_invalid_toml_names_file(home: Path, project: Path) -> None:
    write(home / "forge.toml", "[limits\n")
    with pytest.raises(ConfigError) as exc:
        load_config(project)
    assert "forge.toml" in str(exc.value)


def test_profile_overlays_base(home: Path, project: Path) -> None:
    write(home / "forge.toml", (REPO / "forge.example.toml").read_text(encoding="utf-8"))
    cfg = load_config(project, profile="ci")
    assert cfg.approval.policy == "never"
    assert cfg.limits.max_cost_usd == 1.0
    assert cfg.profile == "ci"
    with pytest.raises(ConfigError, match="unknown profile"):
        load_config(project, profile="nope")


def test_config_model_is_strict() -> None:
    with pytest.raises(ValueError):
        ForgeConfig.model_validate({"limits": {"max_turnz": 1}})


def test_config_check_prints_toml_with_masked_secrets(
    home: Path, project: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    write(
        home / "forge.toml",
        '[providers.corp]\nkind = "openai_compat"\napi_key_env = "CORP_KEY"\n'
        'headers = { Authorization = "Bearer sk-secret-value" }\n',
    )
    monkeypatch.chdir(project)
    assert main(["config", "check"]) == 0
    out = capsys.readouterr().out
    assert "[providers.corp]" in out
    assert 'api_key_env = "CORP_KEY"' in out
    assert "sk-secret-value" not in out
    assert "***" in out
