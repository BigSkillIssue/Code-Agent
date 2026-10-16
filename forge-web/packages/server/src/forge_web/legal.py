"""Legal setup (W34): the admin's terms (AGB), privacy policy, data processing agreement
(AV-Vertrag, with its TOMs and subprocessors) and acceptable use policy, each in versions; the
customers' acceptances; and EU VAT IDs checked with VIES.

The texts come from the admin (their lawyer and tax adviser); Forge Web only keeps them, shows
them to everyone at `/rechtstexte/<kind>` and asks for them. A new version replaces the current
one and must be accepted again. Nothing paid is possible before the customer accepted the current
version of every required text (`billing.required_texts`); each acceptance is kept with its
version and its time.
"""

import re
import secrets
import time
from collections.abc import Sequence
from typing import Any, Literal

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select

from forge_web.audit import audit
from forge_web.auth.sessions import AdminUser, CurrentUser, client_ip
from forge_web.db.billing_models import LegalAcceptance, LegalText, VatCheck
from forge_web.db.engine import Database
from forge_web.db.models import User
from forge_web.hosting.legal_pages import esc, page
from forge_web.services import services_of

Kind = Literal["terms", "privacy", "dpa", "aup"]
TITLES = {"terms": "Allgemeine Geschäftsbedingungen", "privacy": "Datenschutzerklärung",
          "dpa": "Vertrag zur Auftragsverarbeitung", "aup": "Nutzungsregeln"}  # fmt: skip
VAT_ID = re.compile(r"^([A-Z]{2})([0-9A-Z+*]{2,12})$")
# The member states as VIES names them (EL is Greece, XI Northern Ireland).
EU = {
    "AT", "BE", "BG", "CY", "CZ", "DE", "DK", "EE", "EL", "ES", "FI", "FR", "HR", "HU", "IE",
    "IT", "LT", "LU", "LV", "MT", "NL", "PL", "PT", "RO", "SE", "SI", "SK", "XI",
}  # fmt: skip


class TextIn(BaseModel):
    """A new version of a legal text."""

    model_config = ConfigDict(extra="forbid")
    title: str = Field(min_length=1, max_length=200)
    body: str = Field(min_length=1, max_length=200_000)


class AcceptIn(BaseModel):
    """A customer's yes to one version."""

    model_config = ConfigDict(extra="forbid")
    kind: Kind
    version: int = Field(ge=1)


class VatIn(BaseModel):
    """A customer's EU VAT ID to check."""

    model_config = ConfigDict(extra="forbid")
    vat_id: str = Field(min_length=4, max_length=30)


async def current(db: Database) -> dict[str, LegalText]:
    """The newest version of each published text."""
    async with db.session() as session:
        newest = (select(LegalText.kind, func.max(LegalText.version).label("version"))
                  .group_by(LegalText.kind).subquery())  # fmt: skip
        same = (LegalText.kind == newest.c.kind) & (LegalText.version == newest.c.version)
        rows = await session.scalars(select(LegalText).join(newest, same))
        return {row.kind: row for row in rows}


async def missing(db: Database, required: Sequence[str], user: User) -> list[dict[str, Any]]:
    """The required texts whose current version the user has not accepted (unpublished ones
    count as missing: nothing is sold without them)."""
    texts = await current(db)
    async with db.session() as session:
        accepted = {(a.kind, a.version) for a in await session.scalars(
            select(LegalAcceptance).where(LegalAcceptance.user_id == user.id))}  # fmt: skip
    found = []
    for kind in required:
        text = texts.get(kind)
        if text is None:
            found.append({"kind": kind, "version": 0, "title": TITLES.get(kind, kind)})
        elif (kind, text.version) not in accepted:
            found.append({"kind": kind, "version": text.version, "title": text.title})
    return found


async def require_accepted(db: Database, required: Sequence[str], user: User) -> None:
    """409 unless the user accepted the current version of every required text."""
    open_ = await missing(db, required, user)
    if open_:
        names = ", ".join(
            t["kind"] if t["version"] else f"{t['kind']} (not published yet)" for t in open_
        )
        raise HTTPException(409, f"accept these first: {names}")


async def check_vat(url: str, vat_id: str) -> dict[str, Any]:
    """What VIES says about a VAT ID (ValueError for one that cannot be, httpx errors as such)."""
    plain = re.sub(r"[\s.-]", "", vat_id).upper()
    found = VAT_ID.match(plain)
    if found is None or found[1] not in EU or not any(c.isdigit() for c in found[2]):
        raise ValueError("this is not an EU VAT ID (like DE123456789)")
    async with httpx.AsyncClient(timeout=20) as client:
        reply = await client.post(url, json={"countryCode": found[1], "vatNumber": found[2]})
        reply.raise_for_status()
        data = reply.json()
    valid = bool(data.get("valid"))
    return {"vat_id": plain, "valid": valid, "name": registered(valid, data.get("name")),
            "address": registered(valid, data.get("address"))}  # fmt: skip


def registered(valid: bool, text: Any) -> str:
    """What VIES has on record ("---" and anything for an invalid ID mean nothing)."""
    return "" if not valid or text in (None, "---") else str(text)


def legal_texts_router() -> APIRouter:
    """The texts for everyone, the customer's acceptances and VAT ID, and the admin's texts."""
    router = APIRouter()

    @router.get("/rechtstexte/{kind}", response_class=HTMLResponse)
    async def shown(kind: str, request: Request) -> HTMLResponse:
        text = (await current(services_of(request).db)).get(kind)
        if text is None:
            raise HTTPException(404, "no such text")
        day = time.strftime("%d.%m.%Y", time.gmtime(text.published_at))
        return page(text.title, f'<p class="note">Version {text.version} vom {day}</p>'
                                f"<p>{esc(text.body)}</p>")  # fmt: skip

    @router.get("/api/legal/me")
    async def mine(request: Request, user: CurrentUser) -> dict[str, Any]:
        services = services_of(request)
        required = services.settings.billing.required_texts
        async with services.db.session() as session:
            vat = await session.get(VatCheck, user.id)
        return {"missing": await missing(services.db, required, user),
                "vat": vat_view(vat) if vat else None}  # fmt: skip

    @router.post("/api/legal/accept")
    async def accept(body: AcceptIn, request: Request, user: CurrentUser) -> dict[str, bool]:
        services = services_of(request)
        text = (await current(services.db)).get(body.kind)
        if text is None or text.version != body.version:
            raise HTTPException(409, "only the current version can be accepted")
        async with services.db.session() as session, session.begin():
            session.add(LegalAcceptance(id=secrets.token_hex(8), user_id=user.id, kind=body.kind,
                                        version=body.version, accepted_at=time.time(),
                                        ip=client_ip(request)))  # fmt: skip
        await audit(services.db, "legal.accepted", user_id=user.id, ip=client_ip(request),
                    kind=body.kind, version=body.version)  # fmt: skip
        return {"accepted": True}

    @router.post("/api/legal/vat")
    async def vat(body: VatIn, request: Request, user: CurrentUser) -> dict[str, Any]:
        services = services_of(request)
        try:
            found = await check_vat(services.settings.billing.vies_url, body.vat_id)
        except ValueError as error:
            raise HTTPException(422, str(error)) from None
        except httpx.HTTPError as error:
            raise HTTPException(502, f"VIES cannot be asked now: {type(error).__name__}") from None
        async with services.db.session() as session, session.begin():
            row = await session.get(VatCheck, user.id)
            if row is None:
                row = VatCheck(user_id=user.id, vat_id="", checked_at=0.0)
                session.add(row)
            row.vat_id, row.valid = found["vat_id"], found["valid"]
            row.name, row.address, row.checked_at = found["name"], found["address"], time.time()
        return found

    admin_routes(router)
    return router


def admin_routes(router: APIRouter) -> None:
    """`/api/admin/legal`: every version, and publishing a new one."""

    @router.get("/api/admin/legal")
    async def versions(request: Request, admin: AdminUser) -> list[dict[str, Any]]:
        async with services_of(request).db.session() as session:
            rows = await session.scalars(select(LegalText).order_by(
                LegalText.kind, LegalText.version.desc()))  # fmt: skip
            return [text_view(row) for row in rows]

    @router.post("/api/admin/legal/{kind}")
    async def publish(
        kind: Kind, body: TextIn, request: Request, admin: AdminUser
    ) -> dict[str, Any]:
        services = services_of(request)
        async with services.db.session() as session, session.begin():
            last = await session.scalar(select(func.max(LegalText.version)).where(
                LegalText.kind == kind))  # fmt: skip
            row = LegalText(kind=kind, version=(last or 0) + 1, title=body.title.strip(),
                            body=body.body, published_at=time.time(),
                            published_by=admin.id)  # fmt: skip
            session.add(row)
        await audit(services.db, "legal.published", user_id=admin.id, ip=client_ip(request),
                    kind=kind, version=row.version)  # fmt: skip
        return text_view(row)


def text_view(row: LegalText) -> dict[str, Any]:
    """A version of a text for the admin."""
    return {"kind": row.kind, "version": row.version, "title": row.title, "body": row.body,
            "published_at": row.published_at}  # fmt: skip


def vat_view(row: VatCheck) -> dict[str, Any]:
    """A customer's checked VAT ID."""
    return {"vat_id": row.vat_id, "valid": row.valid, "name": row.name, "address": row.address,
            "checked_at": row.checked_at}  # fmt: skip
