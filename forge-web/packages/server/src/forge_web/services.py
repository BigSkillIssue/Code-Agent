"""Everything a request handler needs, built once per app and kept on `app.state`."""

import asyncio
from dataclasses import dataclass, field

from fastapi import Request, WebSocket

from forge_web.auth.oauth_providers import SignIn
from forge_web.auth.ratelimit import AuthLimits
from forge_web.chats.runs import RunManager
from forge_web.containers.driver import ContainerDriver
from forge_web.db.engine import Database
from forge_web.db.writer import EventWriter
from forge_web.egress import Egress
from forge_web.gateway.proxy import Gateway, PrivateServer
from forge_web.hub import Hub
from forge_web.settings import WebSettings
from forge_web.vault import Vault


@dataclass
class Services:
    """The server's long-lived parts."""

    settings: WebSettings
    db: Database
    writer: EventWriter
    driver: ContainerDriver
    hub: Hub
    runs: RunManager
    vault: Vault
    gateway: Gateway
    gateway_server: PrivateServer
    egress: Egress
    sign_in: SignIn
    dev_token: str = ""
    dev_user_id: str = ""
    setup_token: str = ""  # while no account exists: lets the first admin sign up
    limits: AuthLimits = field(default_factory=AuthLimits)
    tasks: list[asyncio.Task[None]] = field(default_factory=list)  # background work
    git_locks: dict[str, asyncio.Lock] = field(default_factory=dict)  # one git job per project


def services_of(connection: Request | WebSocket) -> Services:
    """The services of the app handling this request."""
    found: Services = connection.app.state.services
    return found
