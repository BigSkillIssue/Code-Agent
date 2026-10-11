"""The edge: Caddy in front of every live app, with HTTPS per app.

Caddy gets certificates on demand, but only for hosts this worker says are live apps (its
`ask` endpoint). Production apps answer at `<app>.<apps domain>`, staging at
`<app>.staging.<apps domain>`. The legal pages (`/impressum`, `/datenschutz`) and the
"report content" page (`/melden`) are served from Forge Web for every app, before the app's
own routes, so an app cannot remove or replace them. Everything else goes to the app's
services by their routes, the longest first.
"""

import asyncio
import contextlib
import json
import logging
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx

from forge_hostworker.docker import Docker
from forge_hostworker.runner import HostRunner, network, service_container
from forge_hostworker.wire import DeployPlan

LEGAL_PATHS = ("/impressum", "/datenschutz", "/melden")
ASK_PORT = 47201
CADDY_ADMIN = "http://127.0.0.1:2019"
HSTS = "max-age=31536000"
REFRESH_S = 60  # also brings a restarted Caddy back to the live apps
log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Upstream:
    """Where one route of a live app goes."""

    route: str
    address: str  # ip:port on the app's network


@dataclass(frozen=True)
class LiveApp:
    """An app the edge serves."""

    app: str
    environment: str
    host: str
    upstreams: tuple[Upstream, ...]


def app_host(app: str, environment: str, apps_domain: str) -> str:
    """The host name of an app in an environment."""
    suffix = apps_domain.strip(".").lower()
    return f"{app}.{suffix}" if environment == "production" else f"{app}.staging.{suffix}"


async def live_apps(runner: HostRunner, docker: Docker, apps_domain: str) -> list[LiveApp]:
    """Every app with a live release on this host, with its services' addresses."""
    found: list[LiveApp] = []
    apps = runner.data_dir / "apps"
    for folder in sorted(apps.iterdir()) if apps.is_dir() else []:
        app, _, environment = folder.name.rpartition("-")
        state = runner.state(app, environment)
        plan = runner.saved_plan(app, environment, state.live)
        if plan is None or not await running(docker, plan):
            continue
        upstreams = await addresses(docker, plan)
        if upstreams:
            host = app_host(app, environment, apps_domain)
            found.append(LiveApp(app, environment, host, tuple(upstreams)))
    return found


async def running(docker: Docker, plan: DeployPlan) -> bool:
    """Whether the release's first service runs (a stopped or suspended app is not served)."""
    name = service_container(plan.app, plan.environment, plan.services[0].name, plan.release)
    state = await docker("container", "inspect", "-f", "{{.State.Running}}", name)
    return state.code == 0 and state.out.strip() == "true"


async def addresses(docker: Docker, plan: DeployPlan) -> list[Upstream]:
    """The routed services' addresses, the longest route first."""
    net = network(plan.app, plan.environment)
    template = f'{{{{(index .NetworkSettings.Networks "{net}").IPAddress}}}}'
    found = []
    for service in plan.services:
        if service.route is None:
            continue
        name = service_container(plan.app, plan.environment, service.name, plan.release)
        address = (await docker("inspect", "-f", template, name)).out.strip()
        if address:
            found.append(Upstream(service.route, f"{address}:{service.port}"))
    return sorted(found, key=lambda u: -len(u.route.rstrip("/")))


def caddy_config(apps: list[LiveApp], legal_base: str, ask_port: int = ASK_PORT) -> dict[str, Any]:
    """Caddy's whole JSON configuration for these apps."""
    legal = urlsplit(legal_base)
    routes = [app_route(app, legal.netloc, legal.scheme == "https", legal.path) for app in apps]
    return {
        "admin": {"listen": "127.0.0.1:2019"},
        "apps": {
            "http": {"servers": {"apps": {"listen": [":443"], "routes": routes}}},
            "tls": {
                "automation": {
                    "on_demand": {"permission": {"module": "http",
                                                 "endpoint": f"http://127.0.0.1:{ask_port}/ask"}},
                    "policies": [{"on_demand": True}],
                }
            },
        },
    }  # fmt: skip


def app_route(app: LiveApp, legal_host: str, legal_tls: bool, legal_path: str) -> dict[str, Any]:
    """One host: the legal pages from Forge Web first, then the app's own routes."""
    legal = {
        "match": [{"path": list(LEGAL_PATHS)}],
        "handle": [{
            "handler": "reverse_proxy",
            "upstreams": [{"dial": legal_host if ":" in legal_host else
                           f"{legal_host}:{443 if legal_tls else 80}"}],
            "transport": {"protocol": "http", **({"tls": {}} if legal_tls else {})},
            "rewrite": {"uri": f"{legal_path.rstrip('/')}/{app.app}{{http.request.uri.path}}"},
            "headers": {"request": {"set": {"Host": [legal_host], "X-Forge-App": [app.app]}}},
        }],
        "terminal": True,
    }  # fmt: skip
    own = [
        {
            "match": [
                {"path": ["/*"] if u.route == "/" else [u.route, f"{u.route.rstrip('/')}/*"]}
            ],
            "handle": [{"handler": "reverse_proxy", "upstreams": [{"dial": u.address}]}],
            "terminal": True,
        }
        for u in app.upstreams
    ]
    headers = {"handler": "headers", "response": {"set": {
        "Strict-Transport-Security": [HSTS], "X-Content-Type-Options": ["nosniff"]}}}  # fmt: skip
    return {
        "match": [{"host": [app.host]}],
        "handle": [headers, {"handler": "subroute", "routes": [legal, *own]}],
        "terminal": True,
    }


class Edge:
    """Keeps Caddy's configuration in step with the live apps and answers its TLS questions."""

    def __init__(
        self,
        runner: HostRunner,
        docker: Docker,
        apps_domain: str,
        legal_base: str,
        *,
        admin: str = CADDY_ADMIN,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.runner = runner
        self.docker = docker
        self.apps_domain = apps_domain
        self.legal_base = legal_base
        self.http = httpx.AsyncClient(base_url=admin, timeout=30, transport=transport)
        self.hosts: set[str] = set()

    async def refresh(self) -> list[LiveApp]:
        """Load the current apps into Caddy."""
        apps = await live_apps(self.runner, self.docker, self.apps_domain)
        config = caddy_config(apps, self.legal_base)
        reply = await self.http.post("/load", content=json.dumps(config),
                                     headers={"Content-Type": "application/json"})  # fmt: skip
        reply.raise_for_status()
        self.hosts = {app.host for app in apps}
        return apps

    async def changed(self) -> None:
        """Refresh after a job; Caddy being away is logged, not a failed job."""
        try:
            await self.refresh()
        except (httpx.HTTPError, OSError) as error:
            log.warning("the edge could not reach Caddy: %s", error)

    async def keep_fresh(self, stop: asyncio.Event, every_s: float = REFRESH_S) -> None:
        """Refresh until stopped (Caddy skips a load that changes nothing)."""
        while not stop.is_set():
            await self.changed()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), every_s)

    def allowed(self, domain: str) -> bool:
        """May Caddy get a certificate for this host? Only for a live app's."""
        return domain.strip(".").lower() in self.hosts

    async def serve_ask(self, port: int = ASK_PORT) -> asyncio.Server:
        """Answer `GET /ask?domain=...` on localhost: 200 for live apps, 404 otherwise."""
        return await asyncio.start_server(self.answer, "127.0.0.1", port)

    async def answer(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        """One request of Caddy's (anything but a GET of /ask is a 404)."""
        try:
            line = (await asyncio.wait_for(reader.readline(), 5)).decode("latin-1")
            parts = line.split()
            target = urlsplit(parts[1]) if len(parts) == 3 and parts[0] == "GET" else None
            domain = parse_qs(target.query).get("domain", [""])[0] if target else ""
            ok = target is not None and target.path == "/ask" and self.allowed(domain)
            status = "200 OK" if ok else "404 Not Found"
            writer.write(
                f"HTTP/1.1 {status}\r\nContent-Length: 0\r\nConnection: close\r\n\r\n".encode()
            )
            await writer.drain()
        except (TimeoutError, ConnectionError):
            pass
        finally:
            writer.close()
            with contextlib.suppress(ConnectionError):
                await writer.wait_closed()

    async def close(self) -> None:
        """Stop talking to Caddy."""
        await self.http.aclose()
