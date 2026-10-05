"""Smoke tests: the package imports and the CLI reports its version."""

import subprocess
import sys

import pytest

import forge
from forge.cli import main


def test_import_exposes_version() -> None:
    assert forge.__version__ == "0.1.0"


def test_version_flag_prints_version(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        main(["--version"])
    assert exc.value.code == 0
    assert capsys.readouterr().out.strip() == "forge 0.1.0"


def test_module_entry_point_prints_version() -> None:
    proc = subprocess.run(
        [sys.executable, "-m", "forge", "--version"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0
    assert proc.stdout.strip() == "forge 0.1.0"
