"""Forge Web's product flow (W55): the agent decides what the project becomes, the required
agents check the coders' work, and the user keeps stop, undo and their answers."""

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from forge.app_manifest import load_manifest
from forge.plan import TaskSpec

from forge_sandbox import product_flow
from forge_sandbox.methods import AgentSpec, ChatOptions
from forge_sandbox.product_flow import (
    ProductDecision,
    checkpoints_for_product,
    decide,
    place,
    required_agents,
    required_pass,
    slug,
)
from forge_sandbox.products.place import create
from forge_sandbox.worker import config_overrides
from test_worker import home, root, started

__all__ = ["home", "root"]  # fixtures

SPEC = TaskSpec(goal="a page for a pottery studio", context="", requirements=["opening hours"],
                acceptance_criteria=["the page shows the hours"], size="small")  # fmt: skip
UI = AgentSpec(name="ui", prompt="You are the UI agent.", policy="required", applies_to="ui")
TESTER = AgentSpec(name="tester", prompt="You are the test agent.", policy="required")
SECURITY = AgentSpec(name="security", prompt="You are the security agent.", policy="required")
MATH = AgentSpec(name="math", prompt="You are the math agent.", policy="recommended")


def options(**fields: Any) -> ChatOptions:
    return ChatOptions(product_flow=True, mode="auto", **fields)


async def test_the_agent_decides_what_the_project_becomes(root: Path, home: Path) -> None:
    decision = {"product": "site", "name": "Töpferei Lehm!", "title": "Topferei Lehm",
                "clients": [], "reason": "a page to look at"}  # fmt: skip
    script = {"roles": {"product": [{"text": json.dumps(decision)}, {"text": "no idea"}]},
              "turns": []}  # fmt: skip
    worker, _ = await started(root, options(fake_script=script))
    assert worker.ctx is not None
    found = await decide(worker.ctx, SPEC)
    assert (found.product, found.title) == ("site", "Topferei Lehm")
    assert slug(found.name) == "toepferei-lehm" and slug("2048") == "app-2048"
    note = place(worker.ctx, found, "apps.example")
    assert "written" in note and not isinstance(load_manifest(root / "forge.app.toml"), list)
    assert (root / "docs" / "DESIGN.md").is_file()
    assert (await decide(worker.ctx, SPEC)).product == "none"  # an unreadable answer: nothing
    assert place(worker.ctx, ProductDecision(product="none"), "apps.example") == ""
    await worker.close()


def test_apps_are_added_to_a_product_later(tmp_path: Path) -> None:
    create(tmp_path, "fullstack", "lehm", None, (), "apps.example")
    ctx = SimpleNamespace(root=tmp_path)
    more = ProductDecision(product="fullstack", clients=["android"], payments=True)
    note = place(ctx, more, "apps.example")  # type: ignore[arg-type]
    manifest = load_manifest(tmp_path / "forge.app.toml")
    assert not isinstance(manifest, list) and "android" in manifest.clients
    assert manifest.payments.kind == "relay" and "written" in note
    assert place(ctx, more, "apps.example") == ""  # nothing more to add  # type: ignore[arg-type]
    names = [type(c).__name__ for c in checkpoints_for_product(ctx, apple=True)]  # type: ignore[arg-type]
    assert names == ["AppCheckpoint"]  # no Apple client: no Apple review


def test_required_agents_apply_by_what_the_project_is(tmp_path: Path) -> None:
    ctx = SimpleNamespace(root=tmp_path)
    agents = [TESTER, MATH, SECURITY, UI]
    assert [a.name for a in required_agents(ctx, agents)] == ["tester", "security"]  # type: ignore[arg-type]
    create(tmp_path, "site", "lehm", None, (), "apps.example")
    assert [a.name for a in required_agents(ctx, agents)] == ["ui", "tester", "security"]  # type: ignore[arg-type]


def git(root: Path, *args: str) -> None:
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@x.y", *args], cwd=root,
                   check=True, capture_output=True)  # fmt: skip


def review_world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, ui_spawned: int = 0) -> Any:
    """A committed site and stand-ins for the agents; `ran` lists who ran, in order."""
    create(tmp_path, "site", "lehm", None, (), "apps.example")
    git(tmp_path, "init", "-q")
    git(tmp_path, "add", "-A")
    git(tmp_path, "commit", "-q", "-m", "site")
    ran: list[str] = []
    answers = {"tester": ["the hours are missing on phones", "no findings"],
               "security": ["No findings."]}  # fmt: skip

    async def run_as(ctx: Any, name: str, task: str) -> str:
        ran.append(name)
        return answers[name].pop(0) if name in answers else "designed"

    async def run_agent(ctx: Any, task: str, **_: Any) -> Any:
        ran.append(f"coder: {task.splitlines()[2]}")
        return SimpleNamespace(text="fixed")

    monkeypatch.setattr(product_flow, "run_as", run_as)
    monkeypatch.setattr(product_flow, "run_agent", run_agent)
    monkeypatch.setattr(product_flow, "over_budget", lambda ctx: False)
    spawned = {f"a{i}": SimpleNamespace(role="ui") for i in range(ui_spawned)}
    team = SimpleNamespace(agents=spawned)
    limits = SimpleNamespace(max_turns_per_step=10)
    ctx = SimpleNamespace(root=tmp_path, state=SimpleNamespace(team=team),
                          cfg=SimpleNamespace(limits=limits))  # fmt: skip
    return ctx, ran


async def test_findings_go_back_to_the_coder_until_the_agent_is_satisfied(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx, ran = review_world(tmp_path, monkeypatch)
    start = product_flow.task_start(tmp_path)
    notes = await required_pass(ctx, SPEC, [TESTER, SECURITY, UI], start)
    # nothing of the interface changed since the task began: the UI agent is not called
    assert ran == ["tester", "coder: the hours are missing on phones", "tester", "security"]
    assert notes == []


async def test_the_ui_agent_takes_interface_work_the_lead_did_not_give_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx, ran = review_world(tmp_path, monkeypatch)
    start = product_flow.task_start(tmp_path)
    page = tmp_path / "web" / "src" / "App.tsx"
    page.write_text(page.read_text() + "\n// opening hours\n")
    await required_pass(ctx, SPEC, [UI], start, ui_before=0)
    assert ran == ["ui"]  # the coder changed a page without the UI agent: it looks at it now
    spawned, ran2 = review_world(tmp_path / "other", monkeypatch, ui_spawned=1)
    start2 = product_flow.task_start(tmp_path / "other")
    page2 = tmp_path / "other" / "web" / "src" / "App.tsx"
    page2.write_text(page2.read_text() + "\n// hours\n")
    await required_pass(spawned, SPEC, [UI], start2, ui_before=0)
    assert ran2 == []  # the lead gave the page to the UI agent: no second pass



async def test_the_user_keeps_only_undo(root: Path, home: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    worker, out = await started(root, options(fake_script={"turns": []}))
    assert worker.submit("/mode full-access")
    turn = await out.wait_for("turn")
    assert not turn["ok"] and "only /undo" in turn["summary"]
    overrides = config_overrides(options(), root)
    assert overrides["approval.policy"] == "never"
    assert overrides["roles"]["product"]  # the product decision has the planner's models
    await worker.close()


async def test_the_server_steers_chats_into_the_product_flow(tmp_path: Path) -> None:
    from forge_web.startup import chat_options
    from support import dev_settings

    settings = dev_settings(tmp_path)
    settings.agents.product_flow = True
    chat = SimpleNamespace(mode="ask", model="", effort="high", project_id="p1")
    found = await chat_options(settings)(chat, {"gateway_port": 47101})
    assert (found["mode"], found["product_flow"]) == ("auto", True)
    assert "effort" not in found and found["product_url"] == "http://127.0.0.1:47101"
    ChatOptions.model_validate(found)


async def test_the_product_tab_shows_what_the_agent_made(tmp_path: Path) -> None:
    import httpx

    from support import LiveServer, dev_settings

    settings = dev_settings(tmp_path / "data")
    settings.agents.product_flow = True
    with LiveServer(settings) as server:
        async with httpx.AsyncClient(base_url=server.url, headers=server.headers()) as web:
            me = (await web.get("/api/me")).json()
            assert me["product_flow"] is True and "app" not in me["project_sources"]
            project = (await web.post("/api/projects", json={"name": "Lehm"})).json()["id"]
            url = f"/api/projects/{project}/product"
            assert (await web.get(url)).json() == {"product": False}
            create(server.services.driver.workspace(project), "site", "lehm", None, (), "x.example")
            found = (await web.get(url)).json()
    assert (found["product"], found["kind"], found["clients"]) == (True, "site", ["web"])
    assert found["hosted"] is False and found["deploys"] == []
