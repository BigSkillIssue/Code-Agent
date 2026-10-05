"""Skills: discovered from SKILL.md files, listed in the prompt, read on demand."""

import shutil
from pathlib import Path

import pytest

from forge.agent import run_agent
from forge.config import ForgeConfig
from forge.providers.fake import FakeProvider, FakeToolCall, FakeTurn
from forge.providers.registry import register_provider
from forge.skills import discover, skills_listing
from support import make_ctx

FIXTURES = Path(__file__).parent / "fixtures" / "skills"


@pytest.fixture
def skilled(tmp_project: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    shutil.copytree(FIXTURES / "release", tmp_project / ".forge" / "skills" / "release")
    user = tmp_path / "home" / "skills"
    shutil.copytree(FIXTURES / "changelog", user / "changelog")
    (user / "release").mkdir()
    (user / "release" / "SKILL.md").write_text(
        "---\nname: release\ndescription: user version\n---\nx\n"
    )
    monkeypatch.setenv("FORGE_HOME", str(tmp_path / "home"))
    return tmp_project


def test_skills_list_renders(skilled: Path, tmp_path: Path) -> None:
    skills = discover(skilled)
    assert [s.name for s in skills] == ["changelog", "release"]
    assert skills[0].description == "Writing changelog entries"  # no front matter: first line
    lines = skills_listing(skilled)
    assert lines[1] == (
        "- release: Cut a release - bump the version, update the changelog, tag. "
        "(.forge/skills/release/SKILL.md)"
    )  # the project skill wins over the user one
    assert lines[0].startswith("- changelog: Writing changelog entries (") and lines[0].endswith(
        "changelog/SKILL.md)"
    )


def test_no_skills_no_section(
    tmp_project: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FORGE_HOME", str(tmp_path / "empty"))
    assert skills_listing(tmp_project) == []


async def test_agent_sees_skills_and_reads_the_matching_one(skilled: Path) -> None:
    read = FakeToolCall(name="read_file", arguments={"path": ".forge/skills/release/SKILL.md"})
    fake = FakeProvider(
        [FakeTurn(tool_calls=[read]), FakeTurn(text="Following the release skill.")]
    )
    cfg = ForgeConfig(roles={"coder": ["fake/coder"]})
    register_provider(cfg, fake)
    result = await run_agent(make_ctx(skilled, cfg=cfg), "cut release 1.2.0")
    system = fake.requests[0].system
    assert "When your task matches a skill, read its file with read_file first" in system
    assert "- release: Cut a release" in system and "Group entries" not in system  # names only
    skill = result.messages[2].tool_result
    assert skill is not None and skill.ok and "git tag v<version>" in skill.text
