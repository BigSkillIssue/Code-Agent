"""Customers' own domains for hosted apps (W30).

A project's editor names a domain for an environment of its app; Forge Web answers with a TXT
record to create (`_forge-verify.<domain>` = `forge-verify=<token>`) and where the domain must
point (a CNAME to the app's own host). The check looks the TXT record up over DNS-over-HTTPS
(`hosting.dns_resolver`); only then does the app's host learn the domain (a `domains` job), so the
edge asks for a certificate for verified domains only. A domain belongs to one app; domains under
the apps domain, Forge Web's own and the preview domain are refused.
"""

import re
import secrets
import time
from typing import Any, Literal
from urllib.parse import urlsplit

import httpx
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select

from forge_hostworker.wire import DOMAIN, HostJob, app_host
from forge_web.audit import audit
from forge_web.auth.sessions import CurrentUser, client_ip
from forge_web.db.hosting_models import AppDomain, HostedApp
from forge_web.files_api import allowed
from forge_web.hosting.ops import said
from forge_web.services import Services
from forge_web.settings import WebSettings

TXT_PREFIX = "_forge-verify."
TXT_VALUE = "forge-verify="
MAX_DOMAINS = 10  # per app
DOMAINS_JOB_TIMEOUT_S = 120.0


class DomainIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    domain: str = Field(min_length=4, max_length=253)
    environment: Literal["staging", "production"] = "production"


def normal_domain(value: str, settings: WebSettings) -> str:
    """The domain in its plain form, or ValueError when it cannot be an app's."""
    domain = value.strip().lower().rstrip(".")
    if domain.startswith(("http://", "https://")):
        domain = urlsplit(domain).hostname or ""
    if not DOMAIN.match(domain):
        raise ValueError("this is not a domain name (like shop.example.com)")
    ours = [settings.hosting.apps_domain, settings.preview.domain,
            urlsplit(settings.server.public_url).hostname or ""]  # fmt: skip
    for taken in filter(None, ours):
        if domain == taken or domain.endswith("." + taken):
            raise ValueError(f"domains under {taken} belong to this server")
    return domain


async def txt_records(
    resolver: str, name: str, transport: httpx.AsyncBaseTransport | None = None
) -> list[str]:
    """The TXT records of a name, asked over DNS-over-HTTPS (JSON); [] when there are none."""
    async with httpx.AsyncClient(timeout=10, transport=transport) as client:
        reply = await client.get(resolver, params={"name": name, "type": "TXT"},
                                 headers={"Accept": "application/dns-json"})  # fmt: skip
        reply.raise_for_status()
        answers = reply.json().get("Answer") or []
    texts = []
    for answer in answers:
        if answer.get("type") == 16:
            # A TXT record may come as several quoted strings: "forge-" "verify=..."
            texts.append("".join(re.findall(r'"((?:[^"\\]|\\.)*)"', str(answer.get("data", "")))
                                 or [str(answer.get("data", ""))]))  # fmt: skip
    return texts


def domain_view(row: AppDomain, app: str, apps_domain: str) -> dict[str, Any]:
    """A domain as the go-live page shows it: what to put into DNS, and where it stands."""
    return {"domain": row.domain, "environment": row.environment,
            "txt_name": TXT_PREFIX + row.domain, "txt_value": TXT_VALUE + row.token,
            "points_to": app_host(app, row.environment, apps_domain),
            "verified_at": row.verified_at, "checked_at": row.checked_at,
            "error": row.error}  # fmt: skip


async def send_domains(services: Services, project_id: str, environment: str) -> str:
    """Give the app's host every verified domain of the environment ("" when it has them)."""
    async with services.db.session() as session:
        hosted = await session.get(HostedApp, project_id)
        domains = list(await session.scalars(select(AppDomain.domain).where(
            AppDomain.project_id == project_id, AppDomain.environment == environment,
            AppDomain.verified_at > 0).order_by(AppDomain.domain)))  # fmt: skip
    if hosted is None or hosted.deleting_at:
        return ""
    job = HostJob(id=secrets.token_hex(16), kind="domains", app=hosted.app,
                  environment=environment, domains=domains,
                  timeout_s=DOMAINS_JOB_TIMEOUT_S)  # fmt: skip
    await services.hosts.send(hosted.host_id, "", job)
    result = await services.hosts.result(job.id)
    return "" if result.ok else result.error or "the host did not take the domains"


async def verify(services: Services, row: AppDomain) -> tuple[bool, str]:
    """Whether the domain's TXT record is there, and why not."""
    name = TXT_PREFIX + row.domain
    try:
        found = await txt_records(services.settings.hosting.dns_resolver, name)
    except (httpx.HTTPError, ValueError) as error:
        return False, f"the DNS lookup failed: {error}"
    if TXT_VALUE + row.token in found:
        return True, ""
    return False, f"no TXT record {TXT_VALUE}{row.token} at {name} yet"


def domains_router() -> APIRouter:
    """`/api/projects/{id}/hosting/domains`: the app's own domains (editors change them)."""
    router = APIRouter(prefix="/api/projects/{project_id}/hosting/domains")

    @router.get("")
    async def listed(project_id: str, request: Request, user: CurrentUser) -> list[dict[str, Any]]:
        services = await allowed(request, user, project_id)
        async with services.db.session() as session:
            hosted = await session.get(HostedApp, project_id)
            rows = list(await session.scalars(select(AppDomain).where(
                AppDomain.project_id == project_id).order_by(AppDomain.domain)))  # fmt: skip
        apps_domain = services.settings.hosting.apps_domain
        return [domain_view(r, hosted.app, apps_domain) for r in rows] if hosted else []

    @router.post("")
    async def add(
        project_id: str, body: DomainIn, request: Request, user: CurrentUser
    ) -> dict[str, Any]:
        services = await allowed(request, user, project_id, "editor")
        try:
            domain = normal_domain(body.domain, services.settings)
        except ValueError as error:
            raise HTTPException(422, str(error)) from None
        hosted, row = await added(services, project_id, domain, body.environment)
        await audit(services.db, "hosting.domain_added", user_id=user.id, target=project_id,
                    ip=client_ip(request), domain=domain)  # fmt: skip
        return domain_view(row, hosted.app, services.settings.hosting.apps_domain)

    @router.post("/{domain}/check")
    async def check(
        project_id: str, domain: str, request: Request, user: CurrentUser
    ) -> dict[str, Any]:
        services = await allowed(request, user, project_id, "editor")
        hosted, row = await own_domain(services, project_id, domain)
        ok, error = await verify(services, row)
        async with services.db.session() as session, session.begin():
            stored = await session.get(AppDomain, row.domain)
            if stored is not None:
                stored.checked_at, stored.error = time.time(), error
                stored.verified_at = stored.verified_at or (time.time() if ok else 0.0)
                row = stored
        if ok:
            services.background(said(send_domains(services, project_id, row.environment)))
        return domain_view(row, hosted.app, services.settings.hosting.apps_domain)

    @router.delete("/{domain}")
    async def remove(
        project_id: str, domain: str, request: Request, user: CurrentUser
    ) -> dict[str, bool]:
        services = await allowed(request, user, project_id, "editor")
        _, row = await own_domain(services, project_id, domain)
        async with services.db.session() as session, session.begin():
            stored = await session.get(AppDomain, row.domain)
            if stored is not None:
                await session.delete(stored)
        if row.verified_at:
            services.background(said(send_domains(services, project_id, row.environment)))
        await audit(services.db, "hosting.domain_removed", user_id=user.id, target=project_id,
                    ip=client_ip(request), domain=row.domain)  # fmt: skip
        return {"removed": True}

    return router


async def added(
    services: Services, project_id: str, domain: str, environment: str
) -> tuple[HostedApp, AppDomain]:
    """A new, unverified domain of the app (409 when it cannot have it)."""
    async with services.db.session() as session, session.begin():
        hosted = await session.get(HostedApp, project_id)
        if hosted is None or hosted.deleting_at:
            raise HTTPException(409, "put the app on staging first")
        if await session.get(AppDomain, domain) is not None:
            raise HTTPException(409, "this domain belongs to an app already")
        count = await session.scalar(select(func.count()).select_from(AppDomain).where(
            AppDomain.project_id == project_id))  # fmt: skip
        if (count or 0) >= MAX_DOMAINS:
            raise HTTPException(409, f"an app can have {MAX_DOMAINS} domains")
        row = AppDomain(domain=domain, project_id=project_id, environment=environment,
                        token=secrets.token_hex(16), created_at=time.time())  # fmt: skip
        session.add(row)
    return hosted, row


async def own_domain(
    services: Services, project_id: str, domain: str
) -> tuple[HostedApp, AppDomain]:
    """The app and one of its domains (404 otherwise)."""
    async with services.db.session() as session:
        hosted = await session.get(HostedApp, project_id)
        row = await session.get(AppDomain, domain.lower())
    if hosted is None or row is None or row.project_id != project_id:
        raise HTTPException(404, "no such domain")
    return hosted, row
