"""Custom agents from .forge/agents/*.md and ~/.forge/agents/*.md."""

import shutil
from pathlib import Path

import pytest

from forge.agent import run_agent
from forge.agent_files import AgentFileError, load_agents, parse_agent_file
from forge.config import ForgeConfig
from forge.providers.fake import FakeProvider, FakeToolCall, FakeTurn
from forge.providers.registry import register_provider
from support import make_ctx

FIXTURES = Path(__file__).parent / "fixtures" / "agents"


@pytest.fixture
def agent_dirs(tmp_project: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Project agents in tmp_project/.forge/agents, user agents in $FORGE_HOME/agents."""
    shutil.copytree(FIXTURES / "project", tmp_project / ".forge" / "agents")
    shutil.copytree(FIXTURES / "user", tmp_path / "home" / "agents")
    monkeypatch.setenv("FORGE_HOME", str(tmp_path / "home"))
    return tmp_project


def test_parse_front_matter_and_prompt() -> None:
    agent = parse_agent_file(FIXTURES / "project" / "api-tester.md")
    assert agent.name == "api-tester" and agent.models == ["fake/api-model"]
    assert agent.tools == ["read_file", "grep", "bash"]
    assert agent.prompt.startswith("You write pytest tests")
    listed = parse_agent_file(FIXTURES / "project" / "doc-writer.md")
    assert listed.tools == ["files", "search"] and listed.models == []


def test_project_overrides_user_on_name_clash(agent_dirs: Path) -> None:
    agents = load_agents(agent_dirs)
    assert set(agents) == {"api-tester", "doc-writer"}
    assert agents["doc-writer"].description == "Project version of the doc writer."


def test_invalid_front_matter_names_the_file() -> None:
    path = FIXTURES / "bad" / "broken.md"
    with pytest.raises(AgentFileError, match=r"broken\.md: line 3: expected 'key: value'"):
        parse_agent_file(path)


def test_missing_front_matter(tmp_path: Path) -> None:
    path = tmp_path / "plain.md"
    path.write_text("Just a prompt.\n")
    with pytest.raises(AgentFileError, match=r"plain\.md: must start with front matter"):
        parse_agent_file(path)


async def test_custom_role_runs_with_its_own_model_and_tools(agent_dirs: Path) -> None:
    spawn = FakeToolCall(
        name="spawn_agent", arguments={"role": "api-tester", "task": "test /login"}
    )
    fake = FakeProvider(
        roles={
            "coder": [FakeTurn(tool_calls=[spawn]), FakeTurn(text="ok")],
            "api-model": [FakeTurn(text="3 tests pass")],
        }
    )
    cfg = ForgeConfig(roles={"coder": ["fake/coder"]})
    register_provider(cfg, fake)
    ctx = make_ctx(agent_dirs, cfg=cfg)
    result = await run_agent(ctx, "add tests")
    report = result.messages[2].tool_result
    assert report is not None and "agent a1 (api-tester) finished" in report.text
    assert "3 tests pass" in report.text
    request = next(r for r in fake.requests if r.model == "api-model")
    assert {t.name for t in request.tools} == {"read_file", "grep", "bash"}
    assert "You write pytest tests for HTTP endpoints" in request.system
    assert "Your role: api-tester." in request.system


async def test_bad_agent_file_is_reported_to_the_lead(tmp_project: Path) -> None:
    shutil.copytree(FIXTURES / "bad", tmp_project / ".forge" / "agents")
    spawn = FakeToolCall(name="spawn_agent", arguments={"role": "broken", "task": "x"})
    fake = FakeProvider(roles={"coder": [FakeTurn(tool_calls=[spawn]), FakeTurn(text="ok")]})
    cfg = ForgeConfig(roles={"coder": ["fake/coder"]})
    register_provider(cfg, fake)
    result = await run_agent(make_ctx(tmp_project, cfg=cfg), "go")
    error = result.messages[2].tool_result
    assert error is not None and error.code == "invalid_args" and "broken.md" in error.text
