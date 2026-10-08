"""`forge-web dev-chat` talks to a real local sandbox and prints the answer."""

import subprocess
import sys
from pathlib import Path

import pytest

from forge_web.cli import main


def test_dev_chat_with_the_fake_model(tmp_path: Path) -> None:
    out = subprocess.run(
        [sys.executable, "-m", "forge_web", "--data-dir", str(tmp_path), "dev-chat", "--fake",
         "--prompt", "hi"],
        capture_output=True,
        text=True,
        timeout=120,
    )  # fmt: skip
    assert out.returncode == 0, out.stderr
    assert "Hello from Forge Web's fake model." in out.stdout
    assert (tmp_path / "projects" / "dev-chat" / "forge-home" / "chats" / "dev.json").is_file()


def test_dev_chat_in_an_existing_folder(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    folder = tmp_path / "my-project"
    folder.mkdir()
    args = ["--data-dir", str(tmp_path / "data"), "dev-chat", "--fake", "--workspace", str(folder),
            "--prompt", "hi"]  # fmt: skip
    assert main(args) == 0
    assert "fake model" in capsys.readouterr().out
    assert not (tmp_path / "data" / "projects" / "dev-chat" / "workspace").exists()
