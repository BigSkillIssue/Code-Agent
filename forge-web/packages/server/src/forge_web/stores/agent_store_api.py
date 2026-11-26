"""Forge's agent and the app stores (W58): the store guidelines the admin loaded, the agent's store
review of a commit, and publishing that commit through the stores' APIs once the review passed.

Like the agent's other product routes (`hosting.agent_api`) these live on the gateway, take the
chat's run token and act only on the token's project. Publishing needs the admin's
`stores.agent_publishing` and a passing review of the same commit and store. It uses the person's
own App Store Connect key and Play service account, else the admin's Forge account
(`stores.forge_account`) when one is set. Apple: the commit is approved for Apple in the person's
name (the review is the approval) and uploaded to TestFlight; App Review stays a click on the
release page. Google Play: the newest Android build of the commit goes to the track asked for;
production keeps the admin's approval for people who are not trusted.

A computer-use fallback for what the stores' APIs cannot do is not built: see PROGRESS (W58).
"""

import json
import secrets
import time
from collections.abc import Callable
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from forge_web.audit import audit
from forge_web.db.hosting_models import GoLiveApproval
from forge_web.db.models import AppleApproval, AppStoreKey, Chat, ServerSetting, User
from forge_web.db.store_models import AndroidBuild, PlayKey, StoreReview
from forge_web.gateway.proxy import Gateway, Refusal
from forge_web.services import Services

Store = Literal["apple", "google_play"]
GUIDELINES = "store_guidelines"  # the server setting with the admin's texts, by store
MAX_GUIDELINES = 400_000  # characters of one store's guidelines


class ReviewIn(BaseModel):
    """The store-review agent's verdict on a commit."""

    model_config = ConfigDict(extra="forbid")
    store: Store
    commit: str = Field(pattern=r"^[0-9a-f]{40,64}$")
    verdict: Literal["pass", "concern", "violation"]
    summary: str = Field(default="", max_length=4000)
    findings: list[dict[str, Any]] = Field(default_factory=list, max_length=200)


class PublishIn(BaseModel):
    """A commit to publish to a store."""

    model_config = ConfigDict(extra="forbid")
    store: Store
    commit: str = Field(pattern=r"^[0-9a-f]{40,64}$")
    platforms: list[Literal["ios", "macos", "watchos"]] = Field(default=["ios"], max_length=3)
    track: Literal["internal", "alpha", "beta", "production"] = "internal"
    notes: str = Field(default="", max_length=500)


async def guidelines_of(services: Services) -> dict[str, str]:
    """The admin's store guidelines, by store."""
    async with services.db.session() as session:
        row = await session.get(ServerSetting, GUIDELINES)
    try:
        found = json.loads(row.value) if row is not None else {}
    except ValueError:
        return {}
    return (
        {k: str(v) for k, v in found.items() if isinstance(v, str)}
        if isinstance(found, dict)
        else {}
    )


async def key_owner(services: Services, user: User, store: str) -> User:
    """Whose store account publishes: the person's own, else the admin's Forge account."""
    table = AppStoreKey if store == "apple" else PlayKey
    async with services.db.session() as session:
        if await session.get(table, user.id) is not None:
            return user
        fallback = services.settings.stores.forge_account
        owner = await session.get(User, fallback) if fallback else None
        if owner is not None and await session.get(table, owner.id) is not None:
            return owner
    name = "App Store Connect key" if store == "apple" else "Google Play service account"
    raise HTTPException(
        409,
        f"no {name}: the person adds theirs in Settings, or the admin sets "
        "the Forge account (stores.forge_account)",
    )


async def passed(services: Services, project_id: str, store: str, commit: str) -> StoreReview:
    """The newest review of this commit for this store, if it passed (409 otherwise)."""
    async with services.db.session() as session:
        review = await session.scalar(
            select(StoreReview)
            .where(
                StoreReview.project_id == project_id,
                StoreReview.store == store,
                StoreReview.commit == commit,
            )
            .order_by(StoreReview.created_at.desc())
            .limit(1)
        )
    if review is None or review.verdict != "pass":
        raise HTTPException(
            409, "run the store review of this commit first; publishing needs it to pass"
        )
    return review


async def publish_apple(
    services: Services, chat: Chat, user: User, body: PublishIn, review: StoreReview
) -> list[dict[str, Any]]:
    """Approve the commit for Apple in the person's name and upload it to TestFlight."""
    from forge_web.apple.approvals import project_state
    from forge_web.apple.release_api import new_release

    owner = await key_owner(services, user, "apple")
    commit, clean = await project_state(services, chat.project_id)
    if commit != body.commit or not clean:
        raise HTTPException(409, "commit every change first: the project's HEAD is what ships")
    approval = AppleApproval(
        id=secrets.token_hex(16),
        project_id=chat.project_id,
        chat_id=chat.id,
        user_id=user.id,
        request_id=f"store-review:{review.id}",
        commit=commit,
        clean=True,
        summary=review.summary[:4000],
        created_at=time.time(),
    )
    async with services.db.session() as session, session.begin():
        session.add(approval)
    rows = [
        await new_release(services, owner, approval, platform)
        for platform in dict.fromkeys(body.platforms)
    ]
    for row in rows:
        services.releases.launch(row.id)
    return [{"release": r.id, "platform": r.platform, "step": r.step} for r in rows]


async def publish_play(
    services: Services, chat: Chat, user: User, body: PublishIn
) -> dict[str, Any]:
    """Send the newest Android build of the commit to a Google Play track."""
    from forge_web.stores.play_api import PlayReleaseIn, new_release

    owner = await key_owner(services, user, "google_play")
    async with services.db.session() as session, session.begin():
        build = await session.scalar(
            select(AndroidBuild)
            .where(
                AndroidBuild.project_id == chat.project_id,
                AndroidBuild.commit == body.commit,
                AndroidBuild.status == "built",
            )
            .order_by(AndroidBuild.created_at.desc())
            .limit(1)
        )
        if build is None:
            raise HTTPException(409, "build the Android app of this commit first (android_build)")
        approved = await session.scalar(
            select(GoLiveApproval.id).where(
                GoLiveApproval.project_id == chat.project_id, GoLiveApproval.commit == body.commit
            )
        )
        if approved is None:  # the store review stands for the person's go-live of this commit
            session.add(
                GoLiveApproval(
                    id=secrets.token_hex(16),
                    project_id=chat.project_id,
                    chat_id=chat.id,
                    user_id=user.id,
                    request_id="store-review",
                    commit=body.commit,
                    clean=True,
                    created_at=time.time(),
                )
            )
    release_in = PlayReleaseIn(build_id=build.id, track=body.track, notes=body.notes)
    row, _ = await new_release(services, owner, chat.project_id, release_in)
    if row.status == "releasing":
        services.play.launch(row.id)
    return {"release": row.id, "track": row.track, "status": row.status}


def store_routes(gateway: Gateway, services_of: Callable[[], Services]) -> APIRouter:
    """The gateway's store routes for the agent's own product."""
    router = APIRouter(prefix="/product/store")

    async def caller(request: Request) -> tuple[Chat, User]:
        try:
            return await gateway.caller(request)
        except Refusal as refusal:
            raise HTTPException(refusal.status, str(refusal)) from None

    @router.get("/guidelines/{store}")
    async def guidelines(store: Store, request: Request) -> dict[str, Any]:
        await caller(request)
        text = (await guidelines_of(services_of())).get(store, "")
        return {"store": store, "text": text[:MAX_GUIDELINES], "missing": not text}

    @router.post("/review")
    async def review(body: ReviewIn, request: Request) -> dict[str, Any]:
        chat, user = await caller(request)
        services = services_of()
        row = StoreReview(
            id=secrets.token_hex(16),
            project_id=chat.project_id,
            chat_id=chat.id,
            store=body.store,
            commit=body.commit,
            verdict=body.verdict,
            summary=body.summary,
            findings=json.dumps(body.findings)[:200_000],
            created_at=time.time(),
        )
        async with services.db.session() as session, session.begin():
            session.add(row)
        await audit(
            services.db,
            "agent.store_review",
            user_id=user.id,
            target=chat.project_id,
            store=body.store,
            commit=body.commit,
            verdict=body.verdict,
        )
        return {"review": row.id, "verdict": row.verdict}

    @router.post("/publish")
    async def publish(body: PublishIn, request: Request) -> dict[str, Any]:
        chat, user = await caller(request)
        services = services_of()
        if not services.settings.stores.agent_publishing:
            raise HTTPException(403, "on this server people start store releases themselves")
        if not (await services.entitlements.of(user)).store_publishing:
            raise HTTPException(403, "the plan does not include publishing to the stores")
        review = await passed(services, chat.project_id, body.store, body.commit)
        if body.store == "apple":
            found: Any = await publish_apple(services, chat, user, body, review)
        else:
            found = await publish_play(services, chat, user, body)
        await audit(
            services.db,
            "agent.store_publish",
            user_id=user.id,
            target=chat.project_id,
            store=body.store,
            commit=body.commit,
        )
        return {"store": body.store, "started": found}

    return router
