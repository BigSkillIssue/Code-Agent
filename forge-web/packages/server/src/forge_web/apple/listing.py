"""The App Store listing in Forge Web (W22c): `/api/projects/{id}/apple/listing`.

Forge drafts the listing after the approval (S61) into the project's
`.forge/out/apple/listing.json`; the reviewer's verdict on it is a guideline review of the stage
"listing". Here the user sees the draft, finishes it (the URLs are always theirs) and saves it.
Every save is checked with Forge's own model, so Apple's limits hold; the saved listing is kept
on the server and is what goes to Apple, whatever the sandbox's file says later.
"""

import json
import time
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from forge.apple_listing import LISTING, StoreListing
from pydantic import ValidationError

from forge_sandbox.mux import ChannelClosed
from forge_sandbox.rpc import RpcError
from forge_web.audit import audit
from forge_web.auth.sessions import CurrentUser, client_ip
from forge_web.containers.driver import SandboxError
from forge_web.db.models import AppleListing
from forge_web.files_api import allowed
from forge_web.services import Services

DRAFT_PATH = LISTING.as_posix()
REQUIRED = ("support_url", "privacy_policy_url")  # Apple asks for both before a submission


def missing(listing: StoreListing) -> list[str]:
    """The fields Apple needs before a submission that the listing does not have yet."""
    return [field for field in REQUIRED if not getattr(listing, field)]


def listing_routes() -> APIRouter:
    """Show, save and redraft the project's listing."""
    router = APIRouter(prefix="/api/projects/{project_id}/apple/listing")

    @router.get("")
    async def shown(project_id: str, request: Request, user: CurrentUser) -> dict[str, Any]:
        services = await allowed(request, user, project_id)
        async with services.db.session() as session:
            row = await session.get(AppleListing, project_id)
        if row is not None:
            listing = StoreListing.model_validate_json(row.data)
            return answer(listing, "saved", row.updated_at)
        return await from_draft(services, project_id)

    @router.get("/draft")
    async def draft(project_id: str, request: Request, user: CurrentUser) -> dict[str, Any]:
        services = await allowed(request, user, project_id)
        return await from_draft(services, project_id)

    @router.put("")
    async def save(
        project_id: str, body: dict[str, Any], request: Request, user: CurrentUser
    ) -> dict[str, Any]:
        services = await allowed(request, user, project_id, "editor")
        try:
            listing = StoreListing.model_validate(body)
        except ValidationError as err:
            raise HTTPException(422, problems_of(err)) from None
        now = time.time()
        async with services.db.session() as session, session.begin():
            row = await session.get(AppleListing, project_id)
            if row is None:
                row = AppleListing(project_id=project_id)
                session.add(row)
            row.data, row.user_id, row.updated_at = listing.model_dump_json(), user.id, now
        await audit(services.db, "apple.listing_saved", user_id=user.id, target=project_id,
                    ip=client_ip(request), name=listing.name)  # fmt: skip
        return answer(listing, "saved", now)

    return router


async def from_draft(services: Services, project_id: str) -> dict[str, Any]:
    """Forge's newest draft from the project, checked; empty when there is none (yet)."""
    try:
        read = await services.runs.call(project_id, "fs.read", {"path": DRAFT_PATH})
    except RpcError as err:
        if err.code == "not_found":
            return {"listing": None, "source": "none", "updated_at": 0, "missing": []}
        raise HTTPException(502, f"the project's sandbox could not read the draft: {err}") from None
    except (ChannelClosed, SandboxError, OSError, TimeoutError) as err:
        raise HTTPException(503, f"the project's sandbox is not reachable: {err}") from None
    text = read.get("text") if isinstance(read, dict) else None
    try:
        listing = StoreListing.model_validate(json.loads(text or ""))
    except (ValueError, ValidationError) as err:
        raise HTTPException(422, f"Forge's draft does not fit Apple's limits: {err}") from None
    return answer(listing, "draft", 0)


def answer(listing: StoreListing, source: str, updated_at: float) -> dict[str, Any]:
    """The listing with where it came from and what Apple still needs."""
    return {"listing": listing.model_dump(mode="json"), "source": source,
            "updated_at": updated_at, "missing": missing(listing)}  # fmt: skip


def problems_of(err: ValidationError) -> str:
    """Pydantic's complaints in one readable line per field."""
    lines = []
    for problem in err.errors():
        where = ".".join(str(part) for part in problem.get("loc", ())) or "listing"
        lines.append(f"{where}: {problem.get('msg', 'not valid')}")
    return "; ".join(lines)[:2000]
