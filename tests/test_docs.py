"""The documentation matches the code: generated reference, links, quick start commands."""

import os
import re
import subprocess
from pathlib import Path

import pytest

from forge.cli import main
from forge.config_docs import config_markdown

REPO = Path(__file__).resolve().parents[1]


def test_config_reference_is_up_to_date() -> None:
    assert (REPO / "docs" / "config.md").read_text(encoding="utf-8") == config_markdown(), (
        "run: uv run forge config schema --markdown > docs/config.md"
    )


def test_every_config_field_is_described() -> None:
    text = config_markdown()
    assert "|  |" not in text  # no empty description cell


@pytest.mark.parametrize("page", ["README.md", "docs/quickstart.md", "docs/extending.md"])
def test_local_links_exist(page: str) -> None:
    path = REPO / page
    for target in re.findall(r"\]\(([^)#:]+)\)", path.read_text(encoding="utf-8")):
        assert (path.parent / target).exists(), f"{page} links to missing {target}"


def test_quickstart_offline_commands(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    for name in list(os.environ):
        if name.startswith("FORGE_"):
            monkeypatch.delenv(name)
    monkeypatch.setenv("FORGE_HOME", str(tmp_path / "home"))
    project = tmp_path / "project"
    project.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=project, check=True)
    monkeypatch.chdir(project)
    assert main(["--fake", "say hello"]) == 0
    assert main(["--yes", "--fake", "run", "--json", "say hello"]) == 0
    assert '"kind":"session_done"' in capsys.readouterr().out
