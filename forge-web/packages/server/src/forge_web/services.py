"""Everything a request handler needs, built once per app and kept on `app.state`."""

from dataclasses import dataclass

from fastapi import Request, WebSocket

from forge_web.chats.runs import RunManager
from forge_web.containers.driver import ContainerDriver
from forge_web.db.engine import Database
from forge_web.db.writer import EventWriter
from forge_web.hub import Hub
from forge_web.settings import WebSettings


@dataclass
class Services:
    """The server's long-lived parts."""

    settings: WebSettings
    db: Database
    writer: EventWriter
    driver: ContainerDriver
    hub: Hub
    runs: RunManager
    dev_token: str = ""
    dev_user_id: str = ""


def services_of(connection: Request | WebSocket) -> Services:
    """The services of the app handling this request."""
    found: Services = connection.app.state.services
    return found
