"""The agents of a chat (W53): which model each of Forge's roles uses, per the customer's plan,
and which agents Forge's lead has (Forge Web's built-ins with the admin's changes, and the admin's
own), with their policy: required ones always run in the product flow, recommended ones are
offered to the lead.

The admin's choices are saved as one server setting (`agents`) and win over forge-web.toml.
"""

import json
import logging
import time
from typing import Any

from fastapi import APIRouter, Request
from pydantic import ValidationError
from sqlalchemy import select

from forge_sandbox.prompts import BUILTIN_AGENTS
from forge_web.audit import audit
from forge_web.auth.sessions import AdminUser, client_ip
from forge_web.billing.entitlements import EntitlementEngine
from forge_web.db.billing_models import CatalogItem
from forge_web.db.engine import Database
from forge_web.db.models import ServerSetting
from forge_web.services import services_of
from forge_web.settings import AgentsSettings, WebSettings

log = logging.getLogger(__name__)
SETTING = "agents"
DEFAULT_TIER = "default"
# The roles an admin sets, and Forge's roles each one stands for.
ROLE_GROUPS: dict[str, tuple[str, ...]] = {
    "planner": ("refiner", "planner", "architect", "product"),
    "coder": ("coder", "lead", "explore", "researcher", "browser"),
    "reviewer": ("reviewer", "release_reviewer", "apple_reviewer"),
    "compressor": ("compressor",),
}


def effective_agents(agents: AgentsSettings) -> list[dict[str, Any]]:
    """Every agent the lead may use: the built-ins with the admin's changes, then the admin's
    own; turned-off ones are left out."""
    builtin = {agent.name: agent for agent in BUILTIN_AGENTS}
    changed = {entry.name: entry for entry in agents.agents}
    found: list[dict[str, Any]] = []
    for name in [*builtin, *(n for n in changed if n not in builtin)]:
        base, entry = builtin.get(name), changed.get(name)
        item = {
            "name": name,
            "description": (entry and entry.description) or (base.description if base else ""),
            "prompt": (entry and entry.prompt) or (base.prompt if base else ""),
            "tools": list(entry.tools) if entry else [],
            "policy": entry.policy if entry else (base.policy if base else "recommended"),
            "applies_to": entry.applies_to if entry else (base.applies_to if base else "always"),
            "builtin": base is not None,
        }
        if item["policy"] != "off" and item["prompt"]:
            found.append(item)
    return found


def chains_for(agents: AgentsSettings, tier: str) -> dict[str, list[str]]:
    """The model chains for one plan: Forge's roles, and each agent by its name. A plan's row
    wins over the default row, role by role."""
    rows = {**agents.roles.get(DEFAULT_TIER, {}), **agents.roles.get(tier, {})}
    chains: dict[str, list[str]] = {}
    for role, chain in rows.items():
        if not chain:
            continue
        for forge_role in ROLE_GROUPS.get(role, (role,)):
            chains[forge_role] = list(chain)
    return chains


async def tier_of(entitlements: EntitlementEngine, project_id: str) -> str:
    """The plan whose models a project's chats use: its owner's plan, else the default row."""
    found = await entitlements.of_project(project_id)
    return found.source.removeprefix("plan:") if found.source.startswith("plan:") else DEFAULT_TIER


async def chat_agents(
    settings: WebSettings, entitlements: EntitlementEngine, project_id: str
) -> dict[str, Any]:
    """What a chat's worker gets: role chains and the agents with their own chains."""
    tier = await tier_of(entitlements, project_id)
    chains = chains_for(settings.agents, tier)
    agents = [
        {key: item[key] for key in ("name", "description", "prompt", "tools", "policy",
                                    "applies_to")} | {"models": chains.get(item["name"], [])}
        for item in effective_agents(settings.agents)
    ]  # fmt: skip
    roles = {
        role: chain for role, chain in chains.items() if role not in {a["name"] for a in agents}
    }
    found: dict[str, Any] = {"roles": roles, "agents": agents}
    parallel = settings.agents.parallel.get(tier) or settings.agents.parallel.get(DEFAULT_TIER)
    if parallel:
        found["max_parallel_agents"] = parallel
    return found


async def load_saved_agents(db: Database, settings: WebSettings) -> None:
    """Apply what admins saved (at startup); a broken entry is logged and skipped."""
    async with db.session() as session:
        row = await session.get(ServerSetting, SETTING)
    if row is None:
        return
    try:
        settings.agents = AgentsSettings.model_validate(json.loads(row.value))
    except (ValueError, ValidationError):
        log.warning("ignoring the saved agents: not valid")


async def plan_ids(db: Database) -> list[str]:
    """The catalog's plans, which may have model rows of their own."""
    async with db.session() as session:
        found = await session.scalars(select(CatalogItem.id).where(CatalogItem.kind == "plan"))
        return sorted(found)


def agents_router() -> APIRouter:
    """`/api/admin/agents`: read and replace the agents and their models."""
    router = APIRouter(prefix="/api/admin")

    async def view(request: Request) -> dict[str, Any]:
        services = services_of(request)
        agents = services.settings.agents
        builtin = {a.name: a for a in BUILTIN_AGENTS}
        return {
            **agents.model_dump(), "tiers": [DEFAULT_TIER, *await plan_ids(services.db)],
            "role_groups": list(ROLE_GROUPS),
            "builtin": [{"name": a.name, "description": a.description, "prompt": a.prompt,
                         "policy": a.policy, "applies_to": a.applies_to}
                        for a in builtin.values()],
        }  # fmt: skip

    @router.get("/agents")
    async def get_agents(request: Request, admin: AdminUser) -> dict[str, Any]:
        return await view(request)

    @router.put("/agents")
    async def put_agents(
        body: AgentsSettings, request: Request, admin: AdminUser
    ) -> dict[str, Any]:
        services = services_of(request)
        async with services.db.session() as session, session.begin():
            row = ServerSetting(key=SETTING, value=body.model_dump_json(),
                                updated_at=time.time(), updated_by=admin.id)  # fmt: skip
            await session.merge(row)
        services.settings.agents = body
        await audit(services.db, "agents_changed", user_id=admin.id, ip=client_ip(request))
        return await view(request)

    return router
