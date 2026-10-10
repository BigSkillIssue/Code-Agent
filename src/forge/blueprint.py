"""The product blueprint (S68): before a full-stack product is planned, the `architect` role
turns the refined request into its entities, API, sign-in, storage, clients, payments and hosting
needs, split into components.

The blueprint is saved as `.forge/out/product/blueprint.json` and, for the people who keep
working on the product, as `docs/architecture.md`. The planner makes one group of steps per
component; where the blueprint and `forge.app.toml` disagree, the plan must fix the manifest.
"""

import json
from dataclasses import replace
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from forge import prompts
from forge.agent import run_agent
from forge.app_manifest import AppManifest, Client, ResourceClass, load_manifest
from forge.ctx import Ctx
from forge.plan import TaskSpec
from forge.runtime.ledger import ReadLedger
from forge.structured import StructuredError, parse_as

ROLE = "architect"
BLUEPRINT = Path(".forge") / "out" / "product" / "blueprint.json"
ARCHITECTURE = Path("docs") / "architecture.md"
TURNS = 30  # reading the template's code and docs
FIX_TURNS = 3


class EntityField(BaseModel):
    """One column of an entity."""

    name: str
    type: str  # e.g. "str", "int", "datetime", "bool", "ref:User"
    required: bool = True
    note: str = ""


class Entity(BaseModel):
    """Something the product stores, with its fields and who owns its rows."""

    name: str
    fields: list[EntityField]
    owner: str = ""  # whose data it is ("the user who made it"), for privacy and deletion
    relations: list[str] = []


class Endpoint(BaseModel):
    """One API route."""

    method: Literal["GET", "POST", "PUT", "PATCH", "DELETE"]
    path: str = Field(pattern=r"^/api/")
    access: Literal["public", "user", "owner", "moderator"]
    purpose: str


class Component(BaseModel):
    """One part of the product that gets its own group of plan steps."""

    name: str
    kind: Literal["server", "web", "apple", "android", "windows", "worker"]
    responsibilities: list[str]


class HostingNeeds(BaseModel):
    """What hosting must provide."""

    resource_class: ResourceClass = "small"
    mail: bool = True
    storage_gb: int = Field(default=0, ge=0, le=100)
    secrets: list[str] = []  # names only


class Blueprint(BaseModel):
    """The product's architecture before it is planned."""

    summary: str
    entities: list[Entity]
    api: list[Endpoint]
    auth: str  # how people sign in, and which roles there are
    storage: str = ""  # files the product keeps, if any
    clients: list[Client] = ["web"]
    payments: Literal["none", "relay"] = "none"
    digital_goods: bool = False
    user_content: bool = False  # people post things others see (report and block needed)
    hosting: HostingNeeds = Field(default_factory=HostingNeeds)
    components: list[Component] = Field(min_length=1)


class BlueprintFailed(Exception):
    """The architect gave no usable blueprint."""


async def make_blueprint(ctx: Ctx, spec: TaskSpec) -> Blueprint:
    """Let the architect turn the refined request into a blueprint (one retry on bad JSON)."""
    architect = replace(ctx, agent_id="architect", role=ROLE, ledger=ReadLedger())
    task = prompts.render("architect_task", material=spec.model_dump_json(indent=2))
    result = await run_agent(architect, task, role=ROLE, max_turns=TURNS)
    if result.stopped == "error":
        raise BlueprintFailed(result.text)
    try:
        return parse_as(result.text, Blueprint)
    except StructuredError as problem:
        retry = prompts.render("fix_json", failure=str(problem))
        again = await run_agent(
            architect, retry, role=ROLE, history=result.messages, max_turns=FIX_TURNS
        )
        if again.stopped == "error":
            raise BlueprintFailed(again.text) from problem
        try:
            return parse_as(again.text, Blueprint)
        except StructuredError as error:
            raise BlueprintFailed(str(error)) from error


def manifest_problems(blueprint: Blueprint, manifest: AppManifest) -> list[str]:
    """Where forge.app.toml does not provide what the blueprint needs."""
    problems: list[str] = []
    if blueprint.payments != manifest.payments.kind:
        problems.append(f'[payments] kind must be "{blueprint.payments}"')
    if blueprint.digital_goods and not manifest.payments.digital_goods:
        problems.append("[payments] digital_goods must be true")
    if blueprint.hosting.storage_gb and manifest.storage is None:
        problems.append(f"[storage] is missing (max_gb = {blueprint.hosting.storage_gb})")
    if blueprint.hosting.mail and manifest.mail is None:
        problems.append("[mail] is missing")
    missing = [c for c in blueprint.clients if c not in manifest.clients]
    if missing:
        problems.append(f"clients must include {', '.join(missing)}")
    secrets = [s for s in blueprint.hosting.secrets if s not in manifest.secrets]
    if secrets:
        problems.append(f"secrets must list {', '.join(secrets)}")
    if blueprint.hosting.resource_class != manifest.resource_class:
        problems.append(f'resource_class must be "{blueprint.hosting.resource_class}"')
    return [f"forge.app.toml: {p}" for p in problems]


def save_blueprint(root: Path, blueprint: Blueprint) -> None:
    """Write blueprint.json for Forge and docs/architecture.md for people."""
    path = root / BLUEPRINT
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(blueprint.model_dump_json(indent=2) + "\n", encoding="utf-8")
    doc = root / ARCHITECTURE
    doc.parent.mkdir(parents=True, exist_ok=True)
    doc.write_text(architecture_markdown(blueprint), encoding="utf-8")


def architecture_markdown(blueprint: Blueprint) -> str:
    """The blueprint as a page for the people who keep working on the product."""
    lines = ["# Architecture", "", blueprint.summary, "", "## Components", ""]
    for component in blueprint.components:
        lines.append(
            f"- **{component.name}** ({component.kind}): " + "; ".join(component.responsibilities)
        )
    lines += ["", "## Data", ""]
    for entity in blueprint.entities:
        owner = f" (owned by {entity.owner})" if entity.owner else ""
        lines.append(f"### {entity.name}{owner}")
        lines += [
            f"- `{f.name}`: {f.type}{'' if f.required else ', optional'}"
            + (f" ({f.note})" if f.note else "")
            for f in entity.fields
        ]
        lines += [f"- relation: {relation}" for relation in entity.relations]
        lines.append("")
    lines += ["## API", "", "| Method | Path | Access | Purpose |", "| --- | --- | --- | --- |"]
    lines += [f"| {e.method} | `{e.path}` | {e.access} | {e.purpose} |" for e in blueprint.api]
    needs = blueprint.hosting
    lines += [
        "",
        "## Sign-in",
        "",
        blueprint.auth,
        "",
        "## Hosting",
        "",
        f"- resource class: {needs.resource_class}",
        f"- mail: {'yes' if needs.mail else 'no'}",
        f"- storage: {needs.storage_gb} GB"
        + (f" ({blueprint.storage})" if blueprint.storage else ""),
        f"- payments: {blueprint.payments}"
        + (", digital goods" if blueprint.digital_goods else ""),
        f"- user content: {'yes (report and block)' if blueprint.user_content else 'no'}",
        f"- clients: {', '.join(blueprint.clients)}",
        f"- secrets: {', '.join(needs.secrets) or 'none'}",
        "",
    ]
    return "\n".join(lines)


def planned_spec(spec: TaskSpec, blueprint: Blueprint, problems: list[str]) -> TaskSpec:
    """The spec the planner gets: one group of steps per component, the manifest's fixes."""
    groups = ", ".join(f"{c.name} ({c.kind})" for c in blueprint.components)
    constraints = [
        f"Follow the blueprint in {ARCHITECTURE.as_posix()} ({BLUEPRINT.as_posix()}); plan one "
        f"group of steps per component, in this order: {groups}; start each step's title with "
        "its component's name.",
        *problems,
    ]
    return spec.model_copy(update={"constraints": [*spec.constraints, *constraints]})


async def blueprint_for(ctx: Ctx, spec: TaskSpec) -> tuple[TaskSpec, Blueprint | None]:
    """Make and save the blueprint and adapt the spec; without one the spec stays as it is."""
    try:
        blueprint = await make_blueprint(ctx, spec)
    except BlueprintFailed as error:
        ctx.state.notes.append(f"No blueprint was made ({error}); the plan follows the request.")
        return spec, None
    save_blueprint(ctx.root, blueprint)
    manifest = load_manifest(ctx.root)
    problems = manifest_problems(blueprint, manifest) if isinstance(manifest, AppManifest) else []
    return planned_spec(spec, blueprint, problems), blueprint


def blueprint_text(blueprint: Blueprint | None) -> str:
    """The blueprint as JSON for a reviewer (empty without one)."""
    return json.dumps(blueprint.model_dump(), indent=2) if blueprint else ""
