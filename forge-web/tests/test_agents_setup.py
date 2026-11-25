"""Agents and models the admin sets (W53): per role and per plan, the built-in agents with the
admin's changes, and whether people may pick a chat's model."""

from pathlib import Path

import pytest
from forge.agent_files import load_agents

from forge_sandbox.agents import write_agent_files
from forge_sandbox.methods import AgentSpec, ChatOptions
from forge_web.agents_setup import chains_for, effective_agents
from forge_web.settings import AgentsSettings
from support import LiveServer, person
from test_admin import server_settings

SONNET, MINI, OPUS = "anthropic/claude-sonnet", "openai/gpt-5-mini", "anthropic/claude-opus"


def test_a_plan_row_wins_role_by_role_and_roles_stand_for_forges() -> None:
    agents = AgentsSettings(roles={
        "default": {"planner": [SONNET], "coder": [SONNET], "tester": [MINI]},
        "pro": {"coder": [OPUS, SONNET]},
    })  # fmt: skip
    pro = chains_for(agents, "pro")
    assert pro["coder"] == pro["lead"] == [OPUS, SONNET]  # the plan's own row
    assert pro["refiner"] == pro["planner"] == pro["architect"] == [SONNET]  # the default row
    assert pro["tester"] == [MINI]  # an agent, by its name
    assert chains_for(agents, "free")["coder"] == [SONNET]


def test_builtins_with_the_admins_changes_and_own_agents() -> None:
    agents = AgentsSettings(agents=[
        {"name": "security", "policy": "off"},
        {"name": "ui", "description": "Our designer", "policy": "required"},
        {"name": "seo", "description": "Search", "prompt": "You are the SEO agent.",
         "policy": "recommended", "applies_to": "ui", "tools": ["read_file"]},
    ])  # fmt: skip
    found = {item["name"]: item for item in effective_agents(agents)}
    assert "security" not in found
    assert found["ui"]["description"] == "Our designer" and "DESIGN.md" in found["ui"]["prompt"]
    assert found["tester"]["policy"] == "required" and found["tester"]["builtin"]
    assert found["seo"] == {
        "name": "seo", "description": "Search", "prompt": "You are the SEO agent.",
        "tools": ["read_file"], "policy": "recommended", "applies_to": "ui", "builtin": False,
    }  # fmt: skip


def test_the_worker_writes_and_removes_agent_files(tmp_path: Path) -> None:
    home, project = tmp_path / "home", tmp_path / "project"
    project.mkdir()
    (home / "agents").mkdir(parents=True)
    (home / "agents" / "mine.md").write_text("---\nname: mine\n---\nYou are mine.\n")
    specs = [AgentSpec(name="ui", prompt="You are the UI agent.", models=[SONNET],
                       policy="required"),
             AgentSpec(name="math", prompt="You are the math agent.")]  # fmt: skip
    write_agent_files(home, specs)
    import os

    os.environ["FORGE_HOME"] = str(home)
    try:
        loaded = load_agents(project)
        assert loaded["ui"].models == [SONNET] and "runs it" in loaded["ui"].description
        write_agent_files(home, specs[:1])  # the admin removed math
        assert set(load_agents(project)) == {"ui", "mine"}  # the user's own file stays
    finally:
        del os.environ["FORGE_HOME"]
    assert ChatOptions.model_validate({"agents": [s.model_dump() for s in specs]}).agents == specs


async def test_admins_set_agents_and_people_lose_the_model_picker(tmp_path: Path) -> None:
    data = tmp_path / "data"
    with LiveServer(server_settings(data)) as server:
        admin = await person(server, "admin", role="admin")
        member = await person(server, "member")
        assert (await member.web.get("/api/admin/agents")).status_code == 403
        view = (await admin.web.get("/api/admin/agents")).json()
        assert view["tiers"] == ["default"] and {"ui", "tester", "security"} <= {
            a["name"] for a in view["builtin"]}  # fmt: skip
        body = {"user_model_choice": False, "roles": {"default": {"coder": [OPUS]}},
                "agents": [{"name": "security", "policy": "off"}]}  # fmt: skip
        saved = await admin.web.request("PUT", "/api/admin/agents", json=body)
        assert saved.status_code == 200, saved.text
        for bad in ({"roles": {"default": {"coder": ["no model"]}}},
                    {"agents": [{"name": "Bad Name"}]}, {"unknown": 1}):  # fmt: skip
            assert (
                await admin.web.request("PUT", "/api/admin/agents", json=bad)
            ).status_code == 422
        assert (await member.web.get("/api/me")).json()["model_choice"] is False
        await admin.web.client.aclose()
        await member.web.client.aclose()
    with LiveServer(server_settings(data)) as restarted:  # saved in the database
        agents = restarted.services.settings.agents
        assert agents.user_model_choice is False and agents.roles["default"]["coder"] == [OPUS]


@pytest.mark.parametrize("choice", [True, False])
async def test_a_chats_options_carry_the_agents(tmp_path: Path, choice: bool) -> None:
    from types import SimpleNamespace

    from forge_web.startup import chat_options

    settings = server_settings(tmp_path / "data")
    settings.agents = AgentsSettings(user_model_choice=choice, roles={"default": {"ui": [OPUS]}})

    class Plans:
        async def of_project(self, project_id: str) -> SimpleNamespace:
            return SimpleNamespace(source="server")

    options_for = chat_options(settings, Plans())  # type: ignore[arg-type]
    chat = SimpleNamespace(mode="auto", model=MINI, effort="auto", project_id="p1")
    options = await options_for(chat, {})
    ui = next(a for a in options["agents"] if a["name"] == "ui")
    assert ui["models"] == [OPUS] and ui["policy"] == "required"
    assert ("model" in options) is choice
    ChatOptions.model_validate(options)  # the worker accepts them
