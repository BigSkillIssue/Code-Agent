"""The device routes of the gateway (W60): Forge's agent starts its product's Android or Windows
app on a device, acts on it (tap, type, swipe, keys) and looks at it (screenshots, the screen's
elements), then stops it. Like the other product routes, every call takes the chat's run token
and acts on that chat's project only; no route takes a session, app or worker from the sandbox.
"""

import secrets
from collections.abc import Callable
from pathlib import Path
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from forge_hostworker.device_wire import ActionKind, DeviceAnswer, Direction, Key
from forge_web.audit import audit
from forge_web.db.hosting_models import HostedApp
from forge_web.db.models import Chat, User
from forge_web.db.store_models import WinWorker
from forge_web.devices.sessions import DeviceSession
from forge_web.gateway.proxy import Gateway, Refusal
from forge_web.services import Services
from forge_web.stores.android import android_app
from forge_web.stores.windows import has_windows_app

Platform = Literal["android", "windows"]
Missing = tuple[str, str] | None  # (error, hint)


class StartIn(BaseModel):
    """A device session of a commit of the project (the sandbox's HEAD)."""

    model_config = ConfigDict(extra="forbid")
    platform: Platform
    commit: str = Field(pattern=r"^[0-9a-f]{40,64}$")


class ActIn(BaseModel):
    """One action on the device; points are pixels of the latest screenshot."""

    model_config = ConfigDict(extra="forbid")
    kind: ActionKind
    x: int | None = Field(default=None, ge=0, le=20_000)
    y: int | None = Field(default=None, ge=0, le=20_000)
    target: str = Field(default="", max_length=500)
    text: str = Field(default="", max_length=5_000)
    submit: bool = False
    direction: Direction | None = None
    key: Key | None = None


def shown(answer: DeviceAnswer, session: DeviceSession) -> dict[str, Any]:
    """An answer for the agent's tool, with the session's state."""
    return answer.model_dump() | {"status": session.status}


def device_routes(gateway: Gateway, services_of: Callable[[], Services]) -> APIRouter:
    """`/product/device/...` on the gateway."""
    router = APIRouter(prefix="/product/device")

    async def caller(request: Request) -> tuple[Chat, User]:
        try:
            return await gateway.caller(request)
        except Refusal as refusal:
            raise HTTPException(refusal.status, str(refusal)) from None

    def session_of(chat: Chat) -> DeviceSession:
        session = services_of().devices.active(chat.project_id)
        if session is None:
            raise HTTPException(409, "no device runs for this project: start one with device_start")
        return session

    @router.post("/start")
    async def start(body: StartIn, request: Request) -> dict[str, Any]:
        chat, user = await caller(request)
        services = services_of()
        devices = services.devices
        running = devices.active(chat.project_id)
        if running is not None:
            if running.platform == body.platform and running.commit == body.commit:
                return running.view()  # the same device again: nothing new to start
            await devices.stop(running)  # a new commit or the other platform: replace it
        session = DeviceSession(id=secrets.token_hex(16), platform=body.platform,
                                project_id=chat.project_id, user_id=user.id, chat_id=chat.id,
                                commit=body.commit)  # fmt: skip
        checks = await may_start(services, user, session)
        devices.add(session, checks)
        await audit(services.db, "agent.device_start", user_id=user.id, target=chat.project_id,
                    chat=chat.id, platform=body.platform, commit=body.commit)  # fmt: skip
        return session.view()

    @router.post("/act")
    async def act(body: ActIn, request: Request) -> dict[str, Any]:
        chat, _user = await caller(request)
        session = session_of(chat)
        answer = await services_of().devices.act(session, **body.model_dump())
        return shown(answer, session)

    @router.post("/stop")
    async def stop(request: Request) -> dict[str, Any]:
        chat, _user = await caller(request)
        session = session_of(chat)
        await services_of().devices.stop(session)
        return session.view()

    @router.get("")
    async def status(request: Request) -> dict[str, Any]:
        chat, _user = await caller(request)
        session = services_of().devices.current(chat.project_id)
        return session.view() if session is not None else {"status": "none"}

    return router


async def may_start(services: Services, user: User, session: DeviceSession) -> Callable[..., Any]:
    """Refuse a session this server or this user cannot have now (403, 409, 429, 503); else
    the check of the packed commit (it must hold the app)."""
    settings = services.settings
    devices = settings.devices
    limit = (await services.entitlements.of(user)).device_minutes
    if limit is not None and await services.devices.minutes_used(user.id) >= limit:
        raise HTTPException(429, f"your {limit:.0f} device minutes of this month are used up")
    if session.platform == "android":
        return await android_start(services, session)
    if not devices.windows or not settings.stores.windows_enabled:
        raise HTTPException(403, "Windows devices are turned off on this server")
    async with services.db.session() as db_session:
        machine = await db_session.scalar(select(WinWorker.id).where(
            WinWorker.enabled.is_(True), WinWorker.signs.is_(False)).limit(1))  # fmt: skip
    if machine is None:
        raise HTTPException(503, "no Windows machine is connected to this server")

    def windows_app(path: Path) -> Missing:
        if has_windows_app(path):
            return None
        return ("the product has no Windows app (windows/App)", "add one with product_add")

    return windows_app


async def android_start(services: Services, session: DeviceSession) -> Callable[..., Any]:
    """The Android checks: the emulator's and the build's images, and the app's host."""
    settings = services.settings
    if not settings.devices.android or not settings.devices.android_image:
        raise HTTPException(403, "Android devices are turned off on this server")
    if not settings.hosting.android_image:
        raise HTTPException(403, "Android builds are turned off on this server")
    async with services.db.session() as db_session:
        hosted = await db_session.get(HostedApp, session.project_id)
    if hosted is None:
        raise HTTPException(409, "the product is not on a host yet: deploy it to staging first")
    session.app, session.host_id = hosted.app, hosted.host_id

    def android(path: Path) -> Missing:
        package = android_app(path)
        if package is None:
            return ("the product has no Android app (android/gradlew)", "add one with product_add")
        session.package = package
        return None

    return android
