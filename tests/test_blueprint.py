"""The product blueprint (S68): the architect turns the refined request into components,
entities, API and hosting needs; the planner follows it, and the plan review reads it."""

import json
from pathlib import Path
from typing import Any

import pytest

from forge.app_manifest import parse_manifest
from forge.app_template import write_app
from forge.blueprint import (
    ARCHITECTURE,
    BLUEPRINT,
    Blueprint,
    BlueprintFailed,
    architecture_markdown,
    blueprint_for,
    make_blueprint,
    manifest_problems,
)
from forge.plan import TaskSpec
from forge.providers.fake import FakeProvider, FakeTurn
from forge.wiring import install_fake
from support import make_ctx

SPEC = TaskSpec(
    goal="A shared shopping list",
    context="",
    requirements=["share lists"],
    acceptance_criteria=["a shared list shows for both"],
    size="medium",
)
BLUEPRINT_JSON: dict[str, Any] = {
    "summary": "Families share shopping lists; items are ticked off live.",
    "entities": [
        {
            "name": "ShoppingList",
            "owner": "the user who made it",
            "fields": [{"name": "title", "type": "str"}, {"name": "owner", "type": "ref:User"}],
            "relations": ["has many Item", "shared with many User"],
        },
        {
            "name": "Item",
            "fields": [{"name": "text", "type": "str"}, {"name": "done", "type": "bool"}],
        },
    ],
    "api": [
        {"method": "GET", "path": "/api/lists", "access": "user", "purpose": "my lists"},
        {"method": "POST", "path": "/api/lists/{id}/items", "access": "owner", "purpose": "add"},
    ],
    "auth": "Email and password; list owners share with other users.",
    "clients": ["web"],
    "hosting": {"resource_class": "small", "mail": True, "storage_gb": 0, "secrets": []},
    "components": [
        {"name": "server", "kind": "server", "responsibilities": ["lists API", "sharing"]},
        {"name": "web", "kind": "web", "responsibilities": ["list pages"]},
    ],
}


def answer(**changes: Any) -> FakeTurn:
    """The architect's answer: the blueprint as JSON."""
    return FakeTurn(text="```json\n" + json.dumps(BLUEPRINT_JSON | changes) + "\n```")


@pytest.fixture
def product(tmp_path: Path) -> Path:
    root = tmp_path / "shop"
    write_app(root, "shop")
    return root


async def test_a_request_becomes_a_blueprint(product: Path) -> None:
    ctx = make_ctx(product)
    fake = install_fake(ctx.cfg, FakeProvider(roles={"architect": [answer()]}))
    blueprint = await make_blueprint(ctx, SPEC)
    assert [c.name for c in blueprint.components] == ["server", "web"]
    assert blueprint.entities[0].owner == "the user who made it"
    request = fake.requests[0]
    assert request.model == "architect" and "architect" in request.system
    assert "A shared shopping list" in "\n".join(m.text() for m in request.messages)
    tools = {t.name for t in request.tools}
    assert {"read_file", "grep"} <= tools and not tools & {"write_file", "edit_file", "bash"}


async def test_the_blueprint_is_saved_for_forge_and_for_people(product: Path) -> None:
    ctx = make_ctx(product)
    install_fake(ctx.cfg, FakeProvider(roles={"architect": [answer()]}))
    _, blueprint = await blueprint_for(ctx, SPEC)
    assert blueprint is not None
    saved = json.loads((product / BLUEPRINT).read_text(encoding="utf-8"))
    assert saved["summary"] == BLUEPRINT_JSON["summary"]
    doc = (product / ARCHITECTURE).read_text(encoding="utf-8")
    assert (
        doc.startswith("# Architecture\n") and "| POST | `/api/lists/{id}/items` | owner |" in doc
    )
    assert "**server** (server): lists API; sharing" in doc


async def test_every_component_gets_its_group_of_steps(product: Path) -> None:
    ctx = make_ctx(product)
    install_fake(ctx.cfg, FakeProvider(roles={"architect": [answer()]}))
    spec, _ = await blueprint_for(ctx, SPEC)
    rule = spec.constraints[0]
    assert "one group of steps per component, in this order: server (server), web (web)" in rule
    assert "docs/architecture.md" in rule


async def test_payments_the_manifest_lacks_are_a_problem(product: Path) -> None:
    ctx = make_ctx(product)
    hosting = {"resource_class": "medium", "mail": True, "storage_gb": 5, "secrets": ["MAPS_KEY"]}
    install_fake(
        ctx.cfg,
        FakeProvider(
            roles={
                "architect": [answer(payments="relay", clients=["web", "apple"], hosting=hosting)]
            }
        ),
    )
    spec, _ = await blueprint_for(ctx, SPEC)
    problems = spec.constraints[1:]
    assert 'forge.app.toml: [payments] kind must be "relay"' in problems
    assert "forge.app.toml: clients must include apple" in problems
    assert "forge.app.toml: secrets must list MAPS_KEY" in problems
    assert 'forge.app.toml: resource_class must be "medium"' in problems
    assert not any("[storage]" in p for p in problems)  # the template has storage


def test_a_matching_manifest_has_no_problems() -> None:
    manifest = parse_manifest(
        'name = "shop"\nresource_class = "small"\n[[services]]\nname = "api"\n'
        'runtime = "python3.12"\ncommand = ["x"]\nport = 8000\n[mail]\n'
    )
    assert not isinstance(manifest, list)
    blueprint = Blueprint.model_validate(BLUEPRINT_JSON)
    assert manifest_problems(blueprint, manifest) == []
    no_mail = manifest.model_copy(update={"mail": None})
    assert manifest_problems(blueprint, no_mail) == ["forge.app.toml: [mail] is missing"]


async def test_invalid_json_is_asked_for_once_more(product: Path) -> None:
    ctx = make_ctx(product)
    fake = install_fake(
        ctx.cfg,
        FakeProvider(
            roles={
                "architect": [
                    FakeTurn(text="Here is my plan: a server and a web client."),
                    answer(),
                ]
            }
        ),
    )
    blueprint = await make_blueprint(ctx, SPEC)
    assert blueprint.summary == BLUEPRINT_JSON["summary"]
    assert len(fake.requests) == 2


async def test_without_a_blueprint_the_plan_follows_the_request(product: Path) -> None:
    ctx = make_ctx(product)
    install_fake(
        ctx.cfg, FakeProvider(roles={"architect": [FakeTurn(text="no"), FakeTurn(text="still no")]})
    )
    with pytest.raises(BlueprintFailed):
        await make_blueprint(ctx, SPEC)
    install_fake(
        ctx.cfg, FakeProvider(roles={"architect": [FakeTurn(text="no"), FakeTurn(text="still no")]})
    )
    spec, blueprint = await blueprint_for(ctx, SPEC)
    assert blueprint is None and spec == SPEC
    assert any("No blueprint was made" in note for note in ctx.state.notes)


def test_the_architecture_page_lists_hosting_needs() -> None:
    blueprint = Blueprint.model_validate(BLUEPRINT_JSON | {"user_content": True})
    page = architecture_markdown(blueprint)
    assert "- user content: yes (report and block)" in page
    assert "- `owner`: ref:User" in page
