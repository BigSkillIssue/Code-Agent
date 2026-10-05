"""Phase 0 gate: Forge fixes the seeded bug in examples/buggy end to end."""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from forge.cli import main

REPO = Path(__file__).resolve().parents[2]
PYTHON = Path(sys.executable).as_posix()


@pytest.fixture
def buggy(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A fresh copy of examples/buggy and an empty ~/.forge."""
    for name in list(os.environ):
        if name.startswith("FORGE_"):
            monkeypatch.delenv(name)
    monkeypatch.setenv("FORGE_HOME", str(tmp_path / "home"))
    project = tmp_path / "buggy"
    shutil.copytree(REPO / "examples" / "buggy", project)
    return project


def suite_passes(project: Path) -> bool:
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider"],
        cwd=project,
        capture_output=True,
        text=True,
    )
    return proc.returncode == 0


def test_forge_fixes_the_seeded_bug(buggy: Path, tmp_path: Path) -> None:
    assert not suite_passes(buggy), "the example must start with a failing test"
    script = json.loads((REPO / "tests" / "fixtures" / "fake" / "fix_buggy.json").read_text())
    for turn in script["turns"]:
        for call in turn.get("tool_calls", []):
            if call["name"] == "bash":  # use this interpreter, not whatever `python` is on PATH
                call["arguments"]["command"] = f'"{PYTHON}" -m pytest -q -p no:cacheprovider'
    path = tmp_path / "script.json"
    path.write_text(json.dumps(script))
    code = main(["--fake", str(path), "--yes", "-C", str(buggy), "Make the failing test pass"])
    assert code == 0
    assert suite_passes(buggy)
    assert "return a + b" in (buggy / "calc.py").read_text()


def test_fake_flag_without_script_uses_hello(
    buggy: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["--fake", "-C", str(buggy), "say hello"]) == 0
    assert "Hello from the fake provider" in capsys.readouterr().out


@pytest.mark.live
@pytest.mark.skipif(not os.environ.get("OPENAI_API_KEY"), reason="OPENAI_API_KEY not set")
def test_live_model_fixes_the_seeded_bug(buggy: Path, tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    model = os.environ.get("OPENAI_LIVE_MODEL", "gpt-5-mini")
    (home / "forge.toml").write_text(
        '[providers.openai]\nbase_url = "https://api.openai.com/v1"\n'
        'api_key_env = "OPENAI_API_KEY"\n'
        f'[roles]\ncoder = ["openai/{model}"]\n'
    )
    code = main(["--yes", "-C", str(buggy), "The tests fail. Fix the bug in calc.py so they pass."])
    assert code == 0
    assert suite_passes(buggy)
