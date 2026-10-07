"""Shared fixtures for Forge Web tests."""

from pathlib import Path

import pytest

from forge_web.settings import WebSettings, load_settings


@pytest.fixture
def data_dir(tmp_path: Path) -> Path:
    """An empty data folder for one test."""
    path = tmp_path / "data"
    path.mkdir()
    return path


@pytest.fixture
def settings(data_dir: Path) -> WebSettings:
    """Default settings rooted in the test's data folder, ignoring the real environment."""
    return load_settings(data_dir / "forge-web.toml", environ={"FORGE_WEB_DATA_DIR": str(data_dir)})
