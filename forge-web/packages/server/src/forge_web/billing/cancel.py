"""Cancelling (W33b1): the cancellation button of §312k BGB, and Stripe's customer portal.

The button is a page of its own, `/kuendigen`, open to everyone without signing in: "Verträge
hier kündigen" leads to a short form (who, which contract, ordinary or for cause) and "Jetzt
kündigen" sends it. A signed-in customer does the same in two clicks on their billing page
(`POST /api/billing/cancel`), without signing in again. Each cancellation is kept with the time
it came in and confirmed at once by mail, with that time and when it takes effect (§312k (4)).
An ordinary cancellation of a customer it can match ends their subscriptions at Stripe at the end
of the paid period; everything else (no matching account, for cause, Stripe away) goes to the
admins, who carry it out on their customers page (W40c, `customers.py`), and the customer still
gets the confirmation of receipt.

The open form proves nothing about who sends it, so its page says only that the cancellation
came in (never whether an account matched or when its period ends), and the confirmation that
goes to the account's own address carries a link that takes the cancellation back while the
paid period lasts: whoever cancels someone else's contract cannot keep it cancelled.
"""

import hashlib
import json
import logging
import re
import secrets
import time
from typing import Any
from urllib.parse import parse_qs

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select

from forge_web.auth.mail import mail_enabled, send_mail
from forge_web.auth.sessions import CurrentUser, client_ip
from forge_web.billing.entitlements import PAID
from forge_web.billing.stripe_client import StripeError
from forge_web.db.billing_models import BillingCustomer, Cancellation, Subscription
from forge_web.db.models import User
from forge_web.hosting.legal_pages import EMAIL, esc, page
from forge_web.services import Services, services_of

log = logging.getLogger(__name__)
PER_HOUR = 10  # cancellations from one address
ADDRESS = re.compile(EMAIL)
MAX_FORM_BYTES = 8 * 1024


class CancelIn(BaseModel):
    """A signed-in customer's cancellation."""

    model_config = ConfigDict(extra="forbid")
    contract: str = Field(default="", max_length=200)  # "": every contract
    extraordinary: bool = False
    reason: str = Field(default="", max_length=5000)


async def cancel(services: Services, name: str, email: str, body: CancelIn, user: User | None,
                 sender: str) -> Cancellation:  # fmt: skip
    """Record a cancellation, carry it out where it can be, and confirm it by mail."""
    signed_in = user is not None
    user = user or await account_of(services, email)
    email = user.email if user and user.email else email  # the account's own spelling
    row = Cancellation(id=secrets.token_hex(8), received_at=time.time(), name=name[:200],
                       email=email[:320], contract=body.contract, extraordinary=body.extraordinary,
                       reason=body.reason, user_id=user.id if user else "", status="manual",
                       sender=sender)  # fmt: skip
    undo = ""
    if user is not None and not body.extraordinary:
        row.effective_at, ended = await end_at_period_end(services, user, body.contract, row.id)
        row.status = "done" if row.effective_at else "manual"
        if row.effective_at and not signed_in:
            undo = secrets.token_urlsafe(24)
            row.undo_hash, row.subscriptions = token_hash(undo), json.dumps(ended)
    async with services.db.session() as session, session.begin():
        session.add(row)
    services.background(confirm(services, row, undo))
    if row.status == "manual":
        services.background(tell_admins(services, row))
    return row


async def account_of(services: Services, email: str) -> User | None:
    """The account with this address, if there is one."""
    async with services.db.session() as session:
        return await session.scalar(select(User).where(func.lower(User.email) == email.lower()))


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


async def end_at_period_end(
    services: Services, user: User, contract: str, request_id: str
) -> tuple[float, list[str]]:
    """End the user's subscriptions (or the one named) at Stripe when their paid period ends;
    when that is (0 when there is nothing to end, or Stripe could not be reached), and which."""
    async with services.db.session() as session:
        rows = list(await session.scalars(select(Subscription).where(
            Subscription.user_id == user.id, Subscription.status.in_(PAID))))  # fmt: skip
    rows = [r for r in rows if not contract or contract in (r.item_id, r.stripe_subscription)]
    client = await services.billing.client()
    if not rows or client is None:
        return 0.0, []
    ended = sorted({r.stripe_subscription for r in rows if r.stripe_subscription})
    try:
        for stripe_id in ended:
            await client.post(f"/v1/subscriptions/{stripe_id}", {"cancel_at_period_end": True},
                              idempotency_key=f"forge-cancel-{request_id}-{stripe_id}")  # fmt: skip
    except StripeError as error:
        log.warning("billing: a cancellation could not reach Stripe: %s", error)
        return 0.0, []
    finally:
        await client.close()
    return max(r.current_period_end for r in rows), ended


def when(moment: float) -> str:
    return time.strftime("%d.%m.%Y um %H:%M UTC", time.gmtime(moment))


def receipt(row: Cancellation) -> str:
    """The confirmation of receipt (§312k (4) BGB)."""
    effect = (f"Sie wird zum {when(row.effective_at)} wirksam (Ende des bezahlten Zeitraums)."
              if row.effective_at else
              "Wann sie wirksam wird, prüfen wir und schreiben es Ihnen umgehend.")  # fmt: skip
    kind = "außerordentliche" if row.extraordinary else "ordentliche"
    contract = row.contract or "alle Verträge"
    return (f"Ihre {kind} Kündigung ist am {when(row.received_at)} bei uns eingegangen.\n\n"
            f"Name: {row.name}\nE-Mail: {row.email}\nVertrag: {contract}\n"
            + (f"Grund: {row.reason}\n" if row.reason else "")
            + f"\n{effect}\n\nVorgang: {row.id}")  # fmt: skip


def received(row: Cancellation) -> str:
    """What the open form's page says: the same whether or not an account matched."""
    kind = "außerordentliche" if row.extraordinary else "ordentliche"
    return (f"Ihre {kind} Kündigung ist am {when(row.received_at)} bei uns eingegangen.\n\n"
            f"Name: {row.name}\nE-Mail: {row.email}\nVertrag: {row.contract or 'alle Verträge'}"
            "\n\nDie Bestätigung, auch wann die Kündigung wirksam wird, schicken wir an diese "
            f"E-Mail-Adresse.\n\nVorgang: {row.id}")  # fmt: skip


async def confirm(services: Services, row: Cancellation, undo: str = "") -> None:
    """The confirmation of receipt by mail, with the link that takes back a cancellation from
    the open form."""
    smtp = services.settings.auth.smtp
    text = receipt(row)
    if undo:
        link = f"{services.settings.base_url()}/kuendigen/zuruecknehmen/{undo}"
        text += ("\n\nWenn diese Kündigung nicht von Ihnen kommt, nehmen Sie sie hier zurück "
                 f"(bis zum Ende des bezahlten Zeitraums): {link}")  # fmt: skip
    if mail_enabled(smtp):
        await send_mail(smtp, row.email, "Ihre Kündigung ist eingegangen", text)


async def tell_admins(services: Services, row: Cancellation) -> None:
    """A cancellation an admin has to carry out."""
    smtp = services.settings.auth.smtp
    async with services.db.session() as session:
        admins = list(await session.scalars(select(User.email).where(User.role == "admin")))
    for to in filter(None, admins):
        if mail_enabled(smtp):
            await send_mail(smtp, to, "Forge: Kündigung zum Bearbeiten", receipt(row))


def cancel_router() -> APIRouter:
    """`/kuendigen` for everyone, and the signed-in customer's own routes."""
    router = APIRouter()

    @router.get("/kuendigen", response_class=HTMLResponse)
    async def step(request: Request, schritt: str = "1") -> HTMLResponse:
        if schritt != "2":
            return page("Verträge kündigen", FIRST)
        return page("Verträge kündigen", form_body({}, []))

    @router.post("/kuendigen", response_class=HTMLResponse)
    async def send(request: Request) -> HTMLResponse:
        services = services_of(request)
        raw = await request.body()
        if len(raw) > MAX_FORM_BYTES:
            raise HTTPException(413, "too long")
        form = {k: v[0].strip() for k, v in parse_qs(raw.decode("utf-8", "replace")).items()}
        missing = [n for n in ("name", "email") if not form.get(n)]
        if missing or not ADDRESS.match(form.get("email", "")):
            return page("Verträge kündigen", form_body(form, missing or ["email"]), status=400)
        sender = hashlib.sha256(client_ip(request).encode()).hexdigest()
        if await recent(services, sender) >= PER_HOUR:
            return page("Verträge kündigen", BUSY, 429)
        body = CancelIn(contract=form.get("contract", "")[:200],
                        extraordinary=form.get("kind") == "extraordinary",
                        reason=form.get("reason", "")[:5000])  # fmt: skip
        row = await cancel(services, form["name"], form["email"], body, None, sender)
        return page("Kündigung eingegangen", f"<p>{esc(received(row))}</p>")

    undo_routes(router)

    @router.post("/api/billing/cancel")
    async def mine(body: CancelIn, request: Request, user: CurrentUser) -> dict[str, Any]:
        services = services_of(request)
        row = await cancel(services, user.name or user.email or "", user.email or "", body, user,
                           hashlib.sha256(user.id.encode()).hexdigest())  # fmt: skip
        return {"id": row.id, "status": row.status, "received_at": row.received_at,
                "effective_at": row.effective_at}  # fmt: skip

    @router.post("/api/billing/portal")
    async def portal(request: Request, user: CurrentUser) -> dict[str, str]:
        services = services_of(request)
        return {"url": await portal_url(services, user)}

    return router


def undo_routes(router: APIRouter) -> None:
    """`/kuendigen/zuruecknehmen/<token>`: the link in the confirmation of an open-form one."""
    url = "/kuendigen/zuruecknehmen/{token}"

    @router.get(url, response_class=HTMLResponse)
    async def ask(token: str) -> HTMLResponse:
        return page("Kündigung zurücknehmen", f'<form method="post" action="/kuendigen/'
                    f'zuruecknehmen/{esc(token)}"><p>Ihre Verträge laufen dann weiter.</p>'
                    '<button type="submit">Kündigung zurücknehmen</button></form>')  # fmt: skip

    @router.post(url, response_class=HTMLResponse)
    async def undo(token: str, request: Request) -> HTMLResponse:
        if not await undone(services_of(request), token):
            return page("Kündigung zurücknehmen", "<p>Dieser Link gilt nicht (mehr).</p>", 404)
        return page("Kündigung zurückgenommen", "<p>Ihre Verträge laufen weiter.</p>")


async def undone(services: Services, token: str) -> bool:
    """Take an open-form cancellation back at Stripe, once, while its period lasts."""
    async with services.db.session() as session, session.begin():
        row = await session.scalar(select(Cancellation).where(
            Cancellation.undo_hash == token_hash(token), Cancellation.undone_at == 0))  # fmt: skip
        if row is None or row.effective_at < time.time():
            return False
        row.undone_at, row.status = time.time(), "undone"
        ended, row_id = json.loads(row.subscriptions or "[]"), row.id
    client = await services.billing.client()
    if client is None:
        return True  # nothing was ended at Stripe without one
    try:
        for stripe_id in ended:
            await client.post(f"/v1/subscriptions/{stripe_id}", {"cancel_at_period_end": False},
                              idempotency_key=f"forge-undo-{row_id}-{stripe_id}")  # fmt: skip
    except StripeError as error:
        log.warning("billing: taking a cancellation back could not reach Stripe: %s", error)
    finally:
        await client.close()
    return True


async def portal_url(services: Services, user: User) -> str:
    """Stripe's customer portal for the user (409 before their first order)."""
    async with services.db.session() as session:
        row = await session.get(BillingCustomer, user.id)
    client = await services.billing.client()
    if row is None or client is None:
        raise HTTPException(409, "there is nothing to manage yet")
    try:
        made = await client.post("/v1/billing_portal/sessions", {
            "customer": row.stripe_customer,
            "return_url": services.settings.base_url() + "/billing"})  # fmt: skip
    except StripeError as error:
        raise HTTPException(502, f"Stripe: {error}") from None
    finally:
        await client.close()
    return str(made["url"])


async def recent(services: Services, sender: str) -> int:
    """Cancellations from one address in the last hour."""
    async with services.db.session() as session:
        count = await session.scalar(select(func.count()).select_from(Cancellation).where(
            Cancellation.sender == sender,
            Cancellation.received_at > time.time() - 3600))  # fmt: skip
    return int(count or 0)


BUSY = (
    "<p>Gerade kamen viele Kündigungen von Ihrer Adresse. "
    "Bitte versuchen Sie es später noch einmal.</p>"
)
FIRST = ('<p>Hier können Sie Verträge mit uns kündigen, ohne sich anzumelden.</p>'
         '<form method="get" action="/kuendigen"><input type="hidden" name="schritt" value="2">'
         '<button type="submit">Verträge hier kündigen</button></form>')  # fmt: skip


def form_body(form: dict[str, str], wrong: list[str]) -> str:
    """The form of the second step (with what was filled in, and what is missing)."""

    def field(name: str, label: str, kind: str = "text") -> str:
        bad = ' class="bad"' if name in wrong else ""
        return (f'<label{bad}>{label}<input type="{kind}" name="{name}" '
                f'value="{esc(form.get(name, ""))}"></label>')  # fmt: skip

    cause = form.get("kind") == "extraordinary"
    ordinary, special = ("", " selected") if cause else (" selected", "")
    kinds = (f'<option value="ordinary"{ordinary}>ordentlich, zum nächstmöglichen Zeitpunkt'
             f'</option><option value="extraordinary"{special}>außerordentlich (bitte Grund '
             "angeben)</option>")  # fmt: skip
    return ('<form method="post" action="/kuendigen">'
            + field("name", "Ihr Name (Pflicht)")
            + field("email", "Ihre E-Mail-Adresse (Pflicht)", "email")
            + field("contract", "Welcher Vertrag (leer: alle)")
            + f'<label>Art der Kündigung<select name="kind">{kinds}</select></label>'
            + '<label>Grund (bei außerordentlicher Kündigung)<textarea name="reason" rows="3">'
            + esc(form.get("reason", "")) + "</textarea></label>"
            + '<button type="submit">Jetzt kündigen</button></form>')  # fmt: skip
