"""What Forge's agent calls to work on its own product on Forge's servers (W54): deploy it, see how
the deploys went, read its logs, fetch its pages, query its database and build its apps.

The routes live on the gateway (the chat's private socket, reached from the sandbox through the
daemon's `forward` channel) and take the chat's run token. Every call acts on the project the
token's chat belongs to and on nothing else: no route takes an app, a project or a host from the
sandbox. Production (deploys, logs, pages, the database) also needs the admin's
`hosting.agent_production`. Every call is in the audit log.
"""

import asyncio
import secrets
from collections.abc import Callable
from typing import Any, Literal
from urllib.parse import urlsplit

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, ConfigDict, Field

from forge_hostworker.wire import HostJob, SqlQuery, app_host
from forge_web.audit import audit
from forge_web.db.hosting_models import HostedApp
from forge_web.db.models import Chat, User
from forge_web.gateway.proxy import Gateway, Refusal
from forge_web.hosting.deploy_api import DeployIn, hosting_overview, new_deploy
from forge_web.services import Services

Environment = Literal["staging", "production"]
JOB_WAIT_S = 90.0
FETCH_TIMEOUT_S = 20.0
MAX_PAGE = 200_000  # characters of a fetched page handed back


class EnvIn(BaseModel):
    """An environment of the product."""

    model_config = ConfigDict(extra="forbid")
    environment: Environment = "staging"


class AgentDeployIn(EnvIn):
    """A deploy of a commit of the project (the sandbox's HEAD)."""

    commit: str = Field(pattern=r"^[0-9a-f]{40,64}$")


class FetchIn(EnvIn):
    """A page of the product, as a visitor gets it."""

    path: str = Field(default="/", max_length=2000, pattern=r"^/")


class SqlIn(EnvIn):
    """SQL for the product's own database."""

    query: str = Field(min_length=1, max_length=20_000)
    write: bool = False


class BuildIn(BaseModel):
    """An app build of a commit."""

    model_config = ConfigDict(extra="forbid")
    commit: str = Field(pattern=r"^[0-9a-f]{40,64}$")


def problem(status: int, error: str) -> JSONResponse:
    """An error the agent's tool shows as it is."""
    return JSONResponse({"error": error}, status_code=status)


def agent_routes(gateway: Gateway, services_of: Callable[[], Services]) -> APIRouter:
    """The gateway's routes for the agent's own product."""
    router = APIRouter(prefix="/product")

    async def caller(request: Request, environment: str = "staging") -> tuple[Chat, User]:
        try:
            chat, user = await gateway.caller(request)
        except Refusal as refusal:
            raise HTTPException(refusal.status, str(refusal)) from None
        settings = services_of().settings
        if environment == "production" and not settings.hosting.agent_production:
            raise HTTPException(403, "on this server the agent works on staging only")
        return chat, user

    async def hosted_app(project_id: str) -> HostedApp:
        async with services_of().db.session() as session:
            hosted = await session.get(HostedApp, project_id)
        if hosted is None:
            raise HTTPException(409, "the product is not hosted yet: deploy it to staging first")
        return hosted

    async def logged(chat: Chat, user: User, action: str, **details: Any) -> None:
        await audit(services_of().db, f"agent.{action}", user_id=user.id, target=chat.project_id,
                    chat=chat.id, **details)  # fmt: skip

    async def host_job(
        hosted: HostedApp, environment: str, kind: str, **fields: Any
    ) -> dict[str, Any]:
        services = services_of()
        job = HostJob(
            id=secrets.token_hex(16),
            kind=kind,
            app=hosted.app,
            environment=environment,
            timeout_s=JOB_WAIT_S,
            **fields,
        )
        await services.hosts.send(hosted.host_id, "", job)
        try:
            result = await asyncio.wait_for(services.hosts.result(job.id), JOB_WAIT_S + 15)
        except TimeoutError:
            raise HTTPException(504, "the app's host did not answer in time") from None
        return {"ok": result.ok, "output": result.log_tail, "error": result.error}

    @router.post("/deploy")
    async def deploy(body: AgentDeployIn, request: Request) -> dict[str, Any]:
        chat, user = await caller(request, body.environment)
        services = services_of()
        row = await new_deploy(services, user, chat.project_id,
                               DeployIn(environment=body.environment, commit=body.commit),
                               agent=True)  # fmt: skip
        services.deploys.launch(row.id)
        await logged(chat, user, "deploy", environment=body.environment, commit=body.commit)
        return {"deploy": row.id, "environment": row.environment, "release": row.release,
                "step": row.step, "status": row.status}  # fmt: skip

    @router.get("/status")
    async def status(request: Request) -> dict[str, Any]:
        chat, user = await caller(request)
        return await hosting_overview(services_of(), chat.project_id, user.id)

    @router.post("/logs")
    async def logs(body: EnvIn, request: Request) -> dict[str, Any]:
        chat, user = await caller(request, body.environment)
        hosted = await hosted_app(chat.project_id)
        await logged(chat, user, "logs", environment=body.environment)
        return await host_job(hosted, body.environment, "logs")

    @router.post("/sql")
    async def sql(body: SqlIn, request: Request) -> dict[str, Any]:
        chat, user = await caller(request, body.environment)
        hosted = await hosted_app(chat.project_id)
        await logged(chat, user, "sql", environment=body.environment, write=body.write,
                     query=body.query[:500])  # fmt: skip
        return await host_job(hosted, body.environment, "sql",
                              sql=SqlQuery(query=body.query, write=body.write))  # fmt: skip

    @router.post("/fetch")
    async def fetch(body: FetchIn, request: Request) -> Response:
        chat, user = await caller(request, body.environment)
        hosted = await hosted_app(chat.project_id)
        domain = services_of().settings.hosting.apps_domain
        if not domain:
            return problem(409, "this server has no apps domain yet")
        host = app_host(hosted.app, body.environment, domain)
        url = f"https://{host}{body.path}"
        if urlsplit(url).hostname != host:  # a path such as "/@other" must not change the host
            return problem(422, "that is not a path of the product")
        await logged(chat, user, "fetch", environment=body.environment, path=body.path[:200])
        return JSONResponse(await fetched(url))

    @router.post("/build/{platform}")
    async def build(platform: Literal["android", "windows"], body: BuildIn,
                    request: Request) -> dict[str, Any]:  # fmt: skip
        chat, user = await caller(request)
        services = services_of()
        if platform == "android":
            from forge_web.stores.android_api import new_build as android_build

            row: Any = await android_build(services, user, chat.project_id, body.commit)
            services.android.launch(row.id)
        else:
            from forge_web.stores.windows_api import new_build as windows_build

            row = await windows_build(services, user, chat.project_id, body.commit)
            services.windows.launch(row.id)
        await logged(chat, user, f"{platform}_build", commit=body.commit)
        return {"build": row.id, "status": row.status, "commit": row.commit}

    return router


async def fetched(url: str) -> dict[str, Any]:
    """A page as a visitor gets it: status, the headers that matter, the text (capped)."""
    keep = ("content-type", "location", "cache-control", "content-security-policy")
    try:
        async with httpx.AsyncClient(timeout=FETCH_TIMEOUT_S, follow_redirects=False) as client:
            response = await client.get(url, headers={"User-Agent": "forge-web-agent"})
    except httpx.HTTPError as err:
        return {"ok": False, "error": f"the product did not answer: {err}"}
    text = response.text
    return {"ok": True, "status": response.status_code,
            "headers": {k: v for k, v in response.headers.items() if k in keep},
            "text": text[:MAX_PAGE], "truncated": len(text) > MAX_PAGE}  # fmt: skip
