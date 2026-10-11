"""The legal pages of every hosted app (W28): its Impressum (DDG § 5) and privacy policy (GDPR
Art. 13), rendered by Forge Web from the operator data the creator enters and served by each
host's edge at fixed paths (`/impressum`, `/datenschutz`), so an app can neither remove nor
replace them. The operator of an app is its creator; the admin is the hosting provider and the
creator's processor. The texts are a careful default for a German operator, who stays
responsible for them.

- `GET|PUT /api/projects/{id}/hosting/operator`: the operator data (production needs it).
- `GET /legal/{app}/impressum`, `GET /legal/{app}/datenschutz`: the pages the edge shows.
"""

import html
import time
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from forge_web.audit import audit
from forge_web.auth.sessions import CurrentUser, client_ip
from forge_web.db.hosting_models import AppOperator, HostedApp
from forge_web.files_api import allowed
from forge_web.services import Services, services_of
from forge_web.settings import HostingSettings

EMAIL = r"^[^@\s<>\"']{1,64}@[^@\s<>\"']{1,255}$"
PAGE_HEADERS = {
    "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "public, max-age=300",
}
STYLE = (
    "body{font:16px/1.5 system-ui,sans-serif;max-width:46rem;margin:2rem auto;padding:0 1rem;"
    "color:#1d1d1f}h1{font-size:1.6rem}h2{font-size:1.15rem;margin-top:1.6rem}"
    "label{display:block;margin-top:.8rem}input,textarea,select{width:100%;font:inherit;"
    "padding:.4rem}button{margin-top:1rem;font:inherit;padding:.5rem 1rem}"
    ".note{color:#555}.bad{color:#b00020}"
)


class OperatorIn(BaseModel):
    """Who runs the app, as its Impressum and privacy page name them."""

    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=2, max_length=200)  # a person's or a company's full name
    representative: str = Field(default="", max_length=300)  # a company: legal form, who acts
    address: str = Field(min_length=5, max_length=500)  # a postal address, not a P.O. box
    email: str = Field(pattern=EMAIL)
    phone: str = Field(default="", max_length=60)
    trade_register: str = Field(default="", max_length=300)  # register court and number
    vat_id: str = Field(default="", max_length=40)
    privacy_email: str = Field(default="", pattern=r"^$|" + EMAIL)
    privacy_extra: str = Field(default="", max_length=8_000)


def operator_view(row: AppOperator | None) -> dict[str, Any] | None:
    """The operator data as the go-live page shows it."""
    if row is None:
        return None
    return {key: getattr(row, key) for key in OperatorIn.model_fields} | {
        "updated_at": row.updated_at
    }


def operator_router() -> APIRouter:
    """The operator data of an app project."""
    router = APIRouter(prefix="/api/projects/{project_id}/hosting")

    @router.get("/operator")
    async def get_operator(
        project_id: str, request: Request, user: CurrentUser
    ) -> dict[str, Any] | None:
        services = await allowed(request, user, project_id)
        async with services.db.session() as session:
            return operator_view(await session.get(AppOperator, project_id))

    @router.put("/operator")
    async def set_operator(
        project_id: str, body: OperatorIn, request: Request, user: CurrentUser
    ) -> dict[str, Any] | None:
        services = await allowed(request, user, project_id, "editor")
        values = {key: value.strip() for key, value in body.model_dump().items()}
        async with services.db.session() as session, session.begin():
            row = await session.get(AppOperator, project_id)
            if row is None:
                row = AppOperator(project_id=project_id, updated_at=0.0, **values)
                session.add(row)
            for key, value in values.items():
                setattr(row, key, value)
            row.updated_at = time.time()
        await audit(services.db, "hosting.operator_set", user_id=user.id, target=project_id,
                    ip=client_ip(request))  # fmt: skip
        return operator_view(row)

    return router


def legal_router() -> APIRouter:
    """The legal pages, public: the edge fetches them for everyone who opens an app."""
    router = APIRouter(prefix="/legal/{app}")

    @router.get("/impressum", response_class=HTMLResponse)
    async def impressum(app: str, request: Request) -> HTMLResponse:
        services = services_of(request)
        operator = await operator_of(services, app)
        return page("Impressum", imprint_body(app, operator, services.settings.hosting))

    @router.get("/datenschutz", response_class=HTMLResponse)
    async def privacy(app: str, request: Request) -> HTMLResponse:
        services = services_of(request)
        operator = await operator_of(services, app)
        return page("Datenschutzerklärung", privacy_body(app, operator, services.settings.hosting))

    return router


async def operator_of(services: Services, app: str) -> AppOperator | None:
    """The operator of the hosted app with this name (404 when there is no such app)."""
    async with services.db.session() as session:
        hosted = await session.scalar(select(HostedApp).where(HostedApp.app == app))
        if hosted is None:
            raise HTTPException(404, "no such app")
        return await session.get(AppOperator, hosted.project_id)


def page(title: str, body: str, status: int = 200) -> HTMLResponse:
    """A whole legal page."""
    text = (f'<!doctype html><html lang="de"><head><meta charset="utf-8"><meta name="viewport" '
            f'content="width=device-width,initial-scale=1"><meta name="robots" content="noindex">'
            f"<title>{esc(title)}</title><style>{STYLE}</style></head><body><h1>{esc(title)}</h1>"
            f"{body}</body></html>")  # fmt: skip
    return HTMLResponse(text, status_code=status, headers=PAGE_HEADERS)


def esc(text: str) -> str:
    """Text for HTML, its line breaks kept."""
    return html.escape(text).replace("\n", "<br>")


def missing(app: str) -> str:
    """What a page says while the operator has not entered their data (only staging can)."""
    return (f'<p class="note">Für „{esc(app)}“ liegen noch keine Angaben zum Betreiber vor. '
            "Dies ist eine Testversion und kein öffentliches Angebot.</p>")  # fmt: skip


def imprint_body(app: str, operator: AppOperator | None, hosting: HostingSettings) -> str:
    """The Impressum's content (DDG § 5)."""
    if operator is None:
        return missing(app)
    parts = [f"<h2>Angaben gemäß § 5 DDG</h2><p>{esc(operator.name)}"]
    if operator.representative:
        parts.append(f"<br>{esc(operator.representative)}")
    parts.append(f"</p><p>{esc(operator.address)}</p><h2>Kontakt</h2><p>E-Mail: "
                 f'<a href="mailto:{esc(operator.email)}">{esc(operator.email)}</a>')  # fmt: skip
    if operator.phone:
        parts.append(f"<br>Telefon: {esc(operator.phone)}")
    parts.append("</p>")
    if operator.trade_register:
        parts.append(f"<h2>Registereintrag</h2><p>{esc(operator.trade_register)}</p>")
    if operator.vat_id:
        parts.append(f"<h2>Umsatzsteuer-ID</h2><p>{esc(operator.vat_id)}</p>")
    parts.append('<h2>Inhalte melden</h2><p>Rechtswidrige Inhalte können Sie '
                 '<a href="/melden">hier melden</a>.</p>')  # fmt: skip
    if hosting.provider_name:
        parts.append(
            f'<p class="note">Technischer Betrieb (Hosting): {esc(hosting.provider_name)}</p>'
        )
    return "".join(parts)


def privacy_body(app: str, operator: AppOperator | None, hosting: HostingSettings) -> str:
    """The privacy policy's content (GDPR Art. 13): who is responsible, what is processed, why,
    by whom, how long, and the rights of the people concerned."""
    if operator is None:
        return missing(app)
    contact = operator.privacy_email or operator.email
    provider = esc(hosting.provider_name) if hosting.provider_name else "ein Hosting-Anbieter"
    if hosting.provider_address:
        provider += f", {esc(hosting.provider_address)}"
    sections = [
        ("1. Verantwortlicher", f"<p>{esc(operator.name)}<br>{esc(operator.address)}<br>E-Mail: "
                                f"{esc(contact)}</p>"),
        ("2. Hosting", f"<p>Diese Anwendung läuft auf Servern von {provider}. Der Anbieter "
                       "verarbeitet die Daten nur in unserem Auftrag (Art. 28 DSGVO).</p>"),
        ("3. Server-Protokolle", "<p>Beim Aufruf werden technisch notwendige Daten verarbeitet "
                                 "(IP-Adresse, Zeitpunkt, aufgerufene Adresse, Browser). Zweck ist "
                                 "der sichere Betrieb; Rechtsgrundlage Art. 6 Abs. 1 lit. f DSGVO. "
                                 "Die Protokolle werden nach spätestens 14 Tagen gelöscht.</p>"),
        ("4. Konto und Inhalte", "<p>Wenn Sie ein Konto anlegen, verarbeiten wir die Angaben, "
                                 "die Sie dabei und bei der Nutzung machen, um die Anwendung "
                                 "bereitzustellen (Art. 6 Abs. 1 lit. b DSGVO), bis Sie Ihr Konto "
                                 "löschen.</p>"),
        ("5. Weitere Verarbeitungen", f"<p>{esc(operator.privacy_extra)}</p>"
         if operator.privacy_extra else "<p>Keine.</p>"),
        ("6. Ihre Rechte", "<p>Sie haben das Recht auf Auskunft, Berichtigung, Löschung, "
                           "Einschränkung der Verarbeitung, Datenübertragbarkeit und Widerspruch "
                           "(Art. 15–21 DSGVO) sowie auf Beschwerde bei einer "
                           "Datenschutz-Aufsichtsbehörde (Art. 77 DSGVO). Schreiben Sie dazu an "
                           f"{esc(contact)}.</p>"),
    ]  # fmt: skip
    return "".join(f"<h2>{title}</h2>{text}" for title, text in sections)


def operator_ready(row: AppOperator | None) -> bool:
    """Whether the operator data holds what an Impressum must say."""
    return bool(row and row.name.strip() and row.address.strip() and row.email.strip())
