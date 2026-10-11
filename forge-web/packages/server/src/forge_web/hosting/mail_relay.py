"""The mail relay (W28): hosted apps send mail only through Forge Web (their hosts' firewall closes
port 25), each with a token of its own per environment and a number of mails a day. A mail
always leaves from the server's own address under the app's name, with the operator's address
to reply to, so an app cannot pretend to be someone else. Only the recipient's domain is kept.

The token is made at the first deploy, kept encrypted, and sealed into every release as the
secret `MAIL_RELAY_TOKEN`; the release's `MAIL_RELAY_URL` is `/api/relay/mail` on this server.
"""

import hmac
import secrets
import smtplib
import time
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select

from forge_web.auth.mail import mail_enabled, send_mail
from forge_web.db.engine import Database
from forge_web.db.hosting_models import AppMail, AppMailLog, AppOperator, HostedApp
from forge_web.settings import WebSettings
from forge_web.vault import Vault

if TYPE_CHECKING:  # the services hold the deploys, which use the tokens made here
    from forge_web.services import Services

TOKEN_PREFIX = "fmr"
TOKEN_SECRET = "MAIL_RELAY_TOKEN"
URL_SETTING = "MAIL_RELAY_URL"
ADDRESS = r"^[^@\s<>\",;:]{1,64}@[A-Za-z0-9.-]{1,253}\.[A-Za-z]{2,63}$"


class MailIn(BaseModel):
    """One plain-text mail to one person."""

    model_config = ConfigDict(extra="forbid")
    to: str = Field(pattern=ADDRESS)
    subject: str = Field(min_length=1, max_length=200, pattern=r"^[^\r\n]+$")
    text: str = Field(min_length=1, max_length=50_000)


def relay_url(settings: WebSettings) -> str:
    """Where releases send their mail."""
    return f"{settings.base_url()}/api/relay/mail"


async def relay_token(
    db: Database, vault: Vault, project_id: str, environment: str, daily_limit: int
) -> str:
    """The app's relay token for this environment: made once, then kept (its limit updated)."""
    async with db.session() as session, session.begin():
        row = await session.get(AppMail, (project_id, environment))
        if row is None:
            token = f"{TOKEN_PREFIX}_{project_id}_{environment}_{secrets.token_urlsafe(32)}"
            row = AppMail(project_id=project_id, environment=environment,
                          token=vault.encrypt(token), daily_limit=daily_limit,
                          created_at=time.time())  # fmt: skip
            session.add(row)
            return token
        row.daily_limit = daily_limit
        return vault.decrypt(row.token)


async def relay_of(db: Database, vault: Vault, authorization: str) -> AppMail | None:
    """The relay whose token the header carries, else None."""
    if not authorization.lower().startswith("bearer "):
        return None
    token = authorization[7:].strip()
    parts = token.split("_", 3)
    if len(parts) != 4 or parts[0] != TOKEN_PREFIX:
        return None
    async with db.session() as session:
        row = await session.get(AppMail, (parts[1], parts[2]))
    if row is None or not hmac.compare_digest(vault.decrypt(row.token), token):
        return None
    return row


def services_of(request: Request) -> "Services":
    """The app's services (as `forge_web.services.services_of`, without importing it)."""
    found: Services = request.app.state.services
    return found


async def current_relay(request: Request) -> AppMail:
    """The relay of the request's token; 401 otherwise."""
    services = services_of(request)
    found = await relay_of(services.db, services.vault, request.headers.get("authorization", ""))
    if found is None:
        raise HTTPException(401, "not a valid mail relay token")
    return found


Relay = Annotated[AppMail, Depends(current_relay)]


def relay_router() -> APIRouter:
    """`POST /api/relay/mail`, for hosted apps."""
    router = APIRouter(prefix="/api/relay")

    @router.post("/mail")
    async def mail(body: MailIn, relay: Relay, request: Request) -> dict[str, bool]:
        services = services_of(request)
        smtp = services.settings.auth.smtp
        if not mail_enabled(smtp):
            raise HTTPException(503, "this server sends no mail (no SMTP is set up)")
        async with services.db.session() as session:
            hosted = await session.get(HostedApp, relay.project_id)
            operator = await session.get(AppOperator, relay.project_id)
            sent = await session.scalar(select(func.count(AppMailLog.id)).where(
                AppMailLog.project_id == relay.project_id,
                AppMailLog.environment == relay.environment,
                AppMailLog.created_at >= day_start()))  # fmt: skip
        if hosted is None or hosted.suspended:
            raise HTTPException(403, "this app may not send mail")
        if (sent or 0) >= relay.daily_limit:
            raise HTTPException(429, f"the app's {relay.daily_limit} mails for today are sent")
        await log_mail(services.db, relay, body.to)
        try:
            await send_mail(smtp, body.to, body.subject, body.text, from_name=hosted.app,
                            reply_to=operator.email if operator else "")  # fmt: skip
        except (OSError, smtplib.SMTPException) as err:
            raise HTTPException(502, f"the mail could not be sent: {err}") from None
        return {"ok": True}

    return router


def day_start() -> float:
    """Midnight (UTC) of today, as a timestamp."""
    now = datetime.now(UTC)
    return now.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()


async def log_mail(db: Database, relay: AppMail, to: str) -> None:
    """Count the mail (before sending it: a failed send still used the quota)."""
    async with db.session() as session, session.begin():
        session.add(AppMailLog(id=secrets.token_hex(8), project_id=relay.project_id,
                               environment=relay.environment, domain=to.rsplit("@", 1)[1].lower(),
                               created_at=time.time()))  # fmt: skip
