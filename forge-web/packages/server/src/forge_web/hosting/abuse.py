"""Reports of illegal content in hosted apps (W28b, DSA Art. 16 and 17).

Every app has a report form at the edge's fixed path `/melden` (served from
`/legal/<app>/melden`). A notice names the place, explains why it is illegal, and (except for
child sexual abuse) gives the notifier's name and email with a statement of good faith; the
notifier gets a confirmation and every admin a mail. An admin decides: dismiss (the notifier hears
so, with the reasons) or suspend the app, which stops it at once and sends its creator a statement
of reasons with the ways to contest it. Decisions are made by people, never automatically.
"""

import hashlib
import secrets
import time
from datetime import UTC, datetime
from typing import Any, Literal
from urllib.parse import parse_qs

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select

from forge_web.audit import audit
from forge_web.auth.mail import mail_enabled, send_mail
from forge_web.auth.sessions import AdminUser, client_ip
from forge_web.db.hosting_models import AbuseNotice, HostedApp
from forge_web.db.models import Project, User
from forge_web.hosting.admin import suspend_app
from forge_web.hosting.legal_pages import esc, page
from forge_web.services import Services, services_of

CATEGORIES = {
    "hate": "Hassrede, Bedrohung oder Aufruf zu Gewalt",
    "child_abuse": "Darstellung sexuellen Kindesmissbrauchs",
    "terror": "Terroristische Inhalte",
    "fraud": "Betrug oder Phishing",
    "copyright": "Verletzung von Urheber- oder Markenrechten",
    "privacy": "Verletzung von Persönlichkeitsrechten oder Datenschutz",
    "other": "Anderer rechtswidriger Inhalt",
}
NO_NAME_NEEDED = {"child_abuse"}  # DSA Art. 16(2)(c)
PER_HOUR = 20  # notices from one address and app (behind an edge: often its host)
MAX_FORM_BYTES = 32 * 1024
EMAIL = r"^[^@\s<>\"']{1,64}@[^@\s<>\"']{1,255}$"


class Notice(BaseModel):
    """A notice as the form sends it."""

    model_config = ConfigDict(extra="ignore")
    location: str = Field(min_length=3, max_length=2_000)
    category: Literal["hate", "child_abuse", "terror", "fraud", "copyright", "privacy", "other"]
    reason: str = Field(min_length=10, max_length=10_000)
    name: str = Field(default="", max_length=200)
    email: str = Field(default="", pattern=r"^$|" + EMAIL)
    good_faith: bool


class Decision(BaseModel):
    """An admin's decision on a notice."""

    model_config = ConfigDict(extra="forbid")
    action: Literal["dismiss", "suspend"]
    reason: str = Field(min_length=5, max_length=4_000)


def problems(form: dict[str, str]) -> tuple[Notice | None, list[str]]:
    """The notice, or what is missing from it (in the form's language)."""
    found: list[str] = []
    if form.get("category") not in CATEGORIES:
        found.append("Bitte wählen Sie, um welche Art von Inhalt es geht.")
    if len(form.get("location", "").strip()) < 3:
        found.append("Bitte geben Sie an, wo der Inhalt steht (Adresse oder genaue Beschreibung).")
    if len(form.get("reason", "").strip()) < 10:
        found.append("Bitte begründen Sie, warum der Inhalt rechtswidrig ist.")
    if form.get("category") not in NO_NAME_NEEDED and not (
        form.get("name", "").strip() and form.get("email", "").strip()
    ):
        found.append("Bitte geben Sie Ihren Namen und Ihre E-Mail-Adresse an.")
    if form.get("good_faith") != "yes":
        found.append("Bitte bestätigen Sie, dass Ihre Angaben nach bestem Wissen richtig sind.")
    if found:
        return None, found
    values = {key: form.get(key, "").strip() for key in ("location", "category", "reason", "name",
                                                         "email")}  # fmt: skip
    try:
        return Notice(**values, good_faith=True), []
    except ValueError:
        return None, ["Bitte prüfen Sie Ihre Angaben (zum Beispiel die E-Mail-Adresse)."]


def report_router() -> APIRouter:
    """The report form, public: the edge shows it to everyone who opens an app."""
    router = APIRouter(prefix="/legal/{app}")

    @router.get("/melden", response_class=HTMLResponse)
    async def form(app: str, request: Request, url: str = "") -> HTMLResponse:
        await hosted_app(services_of(request), app)
        return page("Inhalt melden", form_body(app, {"location": url[:2_000]}, []))

    @router.post("/melden", response_class=HTMLResponse)
    async def report(app: str, request: Request) -> HTMLResponse:
        services = services_of(request)
        hosted = await hosted_app(services, app)
        raw = await request.body()
        if len(raw) > MAX_FORM_BYTES:
            raise HTTPException(413, "the notice is too long")
        form = {key: values[0] for key, values in parse_qs(raw.decode("utf-8", "replace")).items()}
        notice, found = problems(form)
        if notice is None:
            return page("Inhalt melden", form_body(app, form, found), status=400)
        sender = hashlib.sha256(f"{client_ip(request)}|{app}".encode()).hexdigest()
        if await recent(services, sender) >= PER_HOUR:
            busy = (
                "<p>Von Ihrer Adresse kamen gerade viele Meldungen. Bitte später noch einmal.</p>"
            )
            return page("Inhalt melden", busy, status=429)
        row = await store(services, hosted, notice, sender)
        services.background(tell_admins(services, row))
        if notice.email:
            subject = f"Ihre Meldung zu „{app}“ ist eingegangen"
            services.background(mail(services, notice.email, subject, confirmation(row)))
        return page("Danke für Ihre Meldung", f"<p>Ihre Meldung ist eingegangen (Nummer {row.id}). "
                                              "Wir prüfen sie und teilen Ihnen die Entscheidung "
                                              "mit, wenn Sie eine E-Mail-Adresse angegeben haben."
                                              "</p>")  # fmt: skip

    return router


async def hosted_app(services: Services, app: str) -> HostedApp:
    """The hosted app with this name (404 otherwise)."""
    async with services.db.session() as session:
        found = await session.scalar(select(HostedApp).where(HostedApp.app == app))
    if found is None:
        raise HTTPException(404, "no such app")
    return found


def form_body(app: str, form: dict[str, str], found: list[str]) -> str:
    """The report form (with what was missing, when it comes back)."""
    errors = "".join(f'<p class="bad">{esc(text)}</p>' for text in found)
    chosen = form.get("category", "")
    options = "".join(
        f'<option value="{key}"{" selected" if key == chosen else ""}>{esc(label)}</option>'
        for key, label in CATEGORIES.items()
    )

    def value(key: str) -> str:
        return esc(form.get(key, ""))

    return (
        f'<p>Hier melden Sie rechtswidrige Inhalte in „{esc(app)}“ (Art. 16 Digital Services Act). '
        "Wir prüfen jede Meldung und entscheiden sorgfältig.</p>" + errors +
        '<form method="post" action="/melden">'
        '<label>Art des Inhalts<select name="category" required>'
        f'<option value="">Bitte wählen</option>{options}</select></label>'
        f'<label>Wo steht der Inhalt? (Adresse oder genaue Beschreibung)<input name="location" '
        f'value="{value("location")}" required></label>'
        f'<label>Warum ist der Inhalt rechtswidrig?<textarea name="reason" rows="6" required>'
        f'{value("reason")}</textarea></label>'
        f'<label>Ihr Name<input name="name" value="{value("name")}"></label>'
        f'<label>Ihre E-Mail-Adresse<input name="email" type="email" value="{value("email")}">'
        "</label>"
        '<label><input type="checkbox" name="good_faith" value="yes" style="width:auto"> Ich bin '
        "in gutem Glauben davon überzeugt, dass meine Angaben richtig und vollständig sind."
        "</label><button type=\"submit\">Meldung senden</button></form>"
        '<p class="note">Name und E-Mail-Adresse brauchen wir nicht bei Darstellungen sexuellen '
        "Kindesmissbrauchs.</p>"
    )  # fmt: skip


async def recent(services: Services, sender: str) -> int:
    """Notices from this sender in the last hour."""
    async with services.db.session() as session:
        since = time.time() - 3600
        found = await session.scalar(select(func.count(AbuseNotice.id)).where(
            AbuseNotice.sender == sender, AbuseNotice.created_at >= since))  # fmt: skip
    return int(found or 0)


async def store(services: Services, hosted: HostedApp, notice: Notice, sender: str) -> AbuseNotice:
    """Keep the notice for the admins."""
    row = AbuseNotice(id=secrets.token_hex(6), project_id=hosted.project_id, app=hosted.app,
                      location=notice.location, category=notice.category, reason=notice.reason,
                      name=notice.name, email=notice.email, sender=sender, status="open",
                      created_at=time.time())  # fmt: skip
    async with services.db.session() as session, session.begin():
        session.add(row)
    await audit(services.db, "hosting.notice_received", target=hosted.project_id, ip="",
                notice=row.id, category=notice.category)  # fmt: skip
    return row


async def mail(services: Services, to: str, subject: str, body: str) -> None:
    """One mail, when this server sends mail (the panel shows everything anyway)."""
    smtp = services.settings.auth.smtp
    if mail_enabled(smtp) and to:
        await send_mail(smtp, to, subject, body, reply_to=services.settings.hosting.contact_email)


async def tell_admins(services: Services, row: AbuseNotice) -> None:
    """Every admin hears of a new notice."""
    async with services.db.session() as session:
        emails = list(await session.scalars(select(User.email).where(User.role == "admin")))
    text = (f"Eine neue Meldung zu „{row.app}“: {CATEGORIES[row.category]}\nWo: {row.location}\n\n"
            f"Prüfen: {services.settings.base_url()}/admin (Hosting, Meldungen)")  # fmt: skip
    for email in emails:
        await mail(services, email or "", f"Neue Meldung zu {row.app}", text)


def confirmation(row: AbuseNotice) -> str:
    """The notifier's confirmation (DSA Art. 16(4))."""
    return (f"Ihre Meldung (Nummer {row.id}) zu „{row.app}“ ist eingegangen.\n\nGemeldet: "
            f"{CATEGORIES[row.category]}\nWo: {row.location}\n\nWir prüfen sie und teilen Ihnen "
            "unsere Entscheidung mit.")  # fmt: skip


def notice_view(row: AbuseNotice, project: str) -> dict[str, Any]:
    """A notice as the panel shows it."""
    return {"id": row.id, "project_id": row.project_id, "app": row.app, "project": project,
            "location": row.location, "category": row.category, "reason": row.reason,
            "name": row.name, "email": row.email, "status": row.status, "decision": row.decision,
            "created_at": row.created_at, "decided_at": row.decided_at}  # fmt: skip


def notices_router() -> APIRouter:
    """`/api/admin/hosting/notices`: the admins' review of notices."""
    router = APIRouter(prefix="/api/admin/hosting/notices")

    @router.get("")
    async def notices(request: Request, admin: AdminUser) -> list[dict[str, Any]]:
        services = services_of(request)
        async with services.db.session() as session:
            rows = await session.execute(
                select(AbuseNotice, Project.name)
                .join(Project, Project.id == AbuseNotice.project_id)
                .order_by((AbuseNotice.status != "open"), AbuseNotice.created_at.desc())
                .limit(200)
            )  # fmt: skip
            return [notice_view(row, name) for row, name in rows]

    @router.post("/{notice_id}/decide")
    async def decide(
        notice_id: str, body: Decision, request: Request, admin: AdminUser
    ) -> dict[str, Any]:
        services = services_of(request)
        row = await decided(services, notice_id, admin, body)
        if body.action == "suspend":
            await suspend_app(services, row.project_id, f"Meldung {row.id}: {body.reason}")
            services.background(tell_creator(services, row, body.reason))
        services.background(mail(services, row.email, f"Ihre Meldung zu „{row.app}“: Entscheidung",
                                 outcome(row, body)))  # fmt: skip
        await audit(services.db, f"hosting.notice_{body.action}", user_id=admin.id,
                    target=row.project_id, ip=client_ip(request), notice=row.id)  # fmt: skip
        return notice_view(row, "")

    return router


async def decided(services: Services, notice_id: str, admin: User, body: Decision) -> AbuseNotice:
    """Record the decision on an open notice."""
    async with services.db.session() as session, session.begin():
        row = await session.get(AbuseNotice, notice_id)
        if row is None:
            raise HTTPException(404, "no such notice")
        if row.status != "open":
            raise HTTPException(409, "this notice was decided already")
        row.status = "actioned" if body.action == "suspend" else "dismissed"
        row.decision, row.decided_by, row.decided_at = body.reason, admin.id, time.time()
    return row


def outcome(row: AbuseNotice, body: Decision) -> str:
    """What the notifier hears (DSA Art. 16(5))."""
    what = ("Wir haben die App gesperrt." if body.action == "suspend"
            else "Wir haben keine Maßnahme ergriffen.")  # fmt: skip
    return (f"Zu Ihrer Meldung {row.id} zu „{row.app}“ haben wir entschieden: {what}\n\n"
            f"Begründung: {body.reason}\n\nDie Entscheidung hat ein Mensch getroffen.")  # fmt: skip


def statement(row: AbuseNotice, reason: str, when: float) -> str:
    """The creator's statement of reasons (DSA Art. 17)."""
    day = datetime.fromtimestamp(when, UTC).strftime("%d.%m.%Y")
    return (
        f"Ihre App „{row.app}“ wurde am {day} gesperrt; sie ist nicht mehr erreichbar.\n\n"
        f"Anlass: eine Meldung (Nummer {row.id}) nach Art. 16 des Digital Services Act.\n"
        f"Gemeldet: {CATEGORIES[row.category]}\nWo: {row.location}\nBegründung der Meldung: "
        f"{row.reason}\n\nUnsere Entscheidung und ihre Gründe: {reason}\n\n"
        "Die Entscheidung hat ein Mensch getroffen, nicht ein automatisiertes Verfahren.\n\n"
        "Ihre Möglichkeiten: Antworten Sie auf diese Mail, wenn Sie der Entscheidung widersprechen "
        "(internes Beschwerdeverfahren). Außerdem können Sie eine zertifizierte außergerichtliche "
        "Streitbeilegungsstelle anrufen (Art. 21 DSA) oder den Rechtsweg beschreiten."
    )  # fmt: skip


async def tell_creator(services: Services, row: AbuseNotice, reason: str) -> None:
    """Send the app's creator the statement of reasons."""
    async with services.db.session() as session:
        email = await session.scalar(select(User.email).join(Project, Project.owner_id == User.id)
                                     .where(Project.id == row.project_id))  # fmt: skip
    await mail(services, email or "", f"Ihre App „{row.app}“ wurde gesperrt",
               statement(row, reason, row.decided_at))  # fmt: skip
