"""Settings layering: defaults, file, environment, overrides."""

from pathlib import Path

import pytest

from forge_web.settings import SettingsError, default_data_dir, load_settings


def test_defaults(tmp_path: Path) -> None:
    settings = load_settings(
        tmp_path / "missing.toml", environ={"FORGE_WEB_DATA_DIR": str(tmp_path)}
    )
    assert settings.data_dir == tmp_path
    assert settings.server.port == 8420
    assert settings.sandbox.isolation == "docker"
    assert settings.base_url() == "http://127.0.0.1:8420"
    assert settings.database_url() == f"sqlite+aiosqlite:///{tmp_path / 'forge-web.db'}"


def test_file_then_env_then_overrides(tmp_path: Path) -> None:
    file = tmp_path / "forge-web.toml"
    file.write_text('[server]\nport = 9000\npublic_url = "https://forge.example.com/"\n')
    env = {"FORGE_WEB_DATA_DIR": str(tmp_path), "FORGE_WEB_SERVER__HOST": "0.0.0.0"}
    settings = load_settings(file, environ=env, overrides={"sandbox.isolation": "local"})
    assert settings.server.port == 9000
    assert settings.server.host == "0.0.0.0"
    assert settings.sandbox.isolation == "local"
    assert settings.base_url() == "https://forge.example.com"


def test_env_beats_file(tmp_path: Path) -> None:
    file = tmp_path / "forge-web.toml"
    file.write_text("[server]\nport = 9000\n")
    env = {"FORGE_WEB_DATA_DIR": str(tmp_path), "FORGE_WEB_SERVER__PORT": "9100"}
    assert load_settings(file, environ=env).server.port == 9100


def test_unknown_key_names_the_file(tmp_path: Path) -> None:
    file = tmp_path / "forge-web.toml"
    file.write_text("[server]\nprot = 1\n")
    with pytest.raises(SettingsError, match=r"forge-web\.toml"):
        load_settings(file, environ={"FORGE_WEB_DATA_DIR": str(tmp_path)})


def test_bad_toml(tmp_path: Path) -> None:
    file = tmp_path / "forge-web.toml"
    file.write_text("[server\n")
    with pytest.raises(SettingsError):
        load_settings(file, environ={})


def test_default_data_dir_override(tmp_path: Path) -> None:
    assert default_data_dir({"FORGE_WEB_DATA_DIR": str(tmp_path)}) == tmp_path
    assert default_data_dir({}).name == "forge-web"


def test_secret_variables_are_not_settings(tmp_path: Path) -> None:
    env = {
        "FORGE_WEB_DATA_DIR": str(tmp_path),
        "FORGE_WEB_SMTP_PASSWORD": "hunter2",
        "FORGE_WEB_GOOGLE_SECRET": "s3cret",
        "FORGE_WEB_AUTH__SIGNUP": "open",
    }
    assert load_settings(tmp_path / "none.toml", environ=env).auth.signup == "open"


def test_sign_in_providers(tmp_path: Path) -> None:
    file = tmp_path / "forge-web.toml"
    file.write_text(
        '[auth.providers.google]\nclient_id = "g"\n'
        '[auth.providers.firma]\nclient_id = "f"\nissuer = "https://sso.example.com"\n'
    )
    providers = load_settings(file, environ={"FORGE_WEB_DATA_DIR": str(tmp_path)}).auth.providers
    assert providers["google"].provider_kind("google") == "google"
    assert providers["firma"].provider_kind("firma") == "oidc"
