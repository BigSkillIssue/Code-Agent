"""Release packaging: one version everywhere, a changelog entry, the release workflow."""

import tomllib
from pathlib import Path

import forge

ROOT = Path(__file__).resolve().parents[1]


def test_version_is_one_everywhere() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    assert project["version"] == forge.__version__ == "1.0.0"


def test_changelog_has_the_release() -> None:
    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    assert f"## {forge.__version__}" in changelog


def test_release_workflow_builds_and_smoke_tests() -> None:
    workflow = (ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")
    assert 'tags: ["v*"]' in workflow
    for needed in ("uv build", "packaging/pyinstaller.spec", "forge --version"):
        assert needed in workflow
    assert 'run --json --yes "say hi" --fake' in workflow


def test_pyinstaller_spec_starts_the_cli() -> None:
    spec = (ROOT / "packaging" / "pyinstaller.spec").read_text(encoding="utf-8")
    assert "__main__.py" in spec and 'name="forge"' in spec
