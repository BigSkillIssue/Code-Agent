"""A new app project starts from Forge's full-stack template (`forge app new`): a FastAPI
server on PostgreSQL, a React web client, docs, tests and CI, and `forge.app.toml`.

The files are made on the server from the template (no project code runs here), written into
the project's sandbox by its daemon and committed, so the project starts clean.
"""

import base64
import re

from forge.app_manifest import parse_manifest
from forge.app_template import app_files

from forge_web.db.models import User
from forge_web.git_api import committer
from forge_web.services import Services

GERMAN = {"ä": "ae", "ö": "oe", "ü": "ue", "Ä": "Ae", "Ö": "Oe", "Ü": "Ue", "ß": "ss"}
MAX_NAME = 40
MAX_TITLE = 60
MESSAGE = "Start from Forge's full-stack app template"


def spelled(project_name: str) -> str:
    """The name with German letters spelled out ("Zähler" -> "Zaehler")."""
    return "".join(GERMAN.get(c, c) for c in project_name)


def product_name(project_name: str) -> str:
    """The product's slug (its host name later): lowercase letters, digits and dashes,
    starting with a letter ("Mein Zähler" -> "mein-zaehler", "2048" -> "app-2048")."""
    slug = re.sub(r"[^a-z0-9]+", "-", spelled(project_name).lower()).strip("-")
    if not slug or not slug[0].isalpha():
        slug = f"app-{slug}".strip("-")
    return slug[:MAX_NAME].rstrip("-")


def product_title(project_name: str) -> str:
    """The name people see: letters, digits, spaces and dashes ("Mein Zähler!" ->
    "Mein Zaehler"); the slug's words when nothing is left."""
    title = re.sub(r"[^A-Za-z0-9 -]+", " ", spelled(project_name))
    title = re.sub(r"\s+", " ", title).strip(" -")
    if not title or not title[0].isalnum():
        title = " ".join(word.capitalize() for word in product_name(project_name).split("-"))
    return title[:MAX_TITLE].rstrip(" -")


async def write_template(
    services: Services, project_id: str, project_name: str, user: User
) -> None:
    """Write the template's files into the project's sandbox and commit them as the user."""
    files = app_files(product_name(project_name), product_title(project_name))
    for path, data in files.items():
        params = {"path": path, "base64": base64.b64encode(data).decode(), "create_dirs": True}
        await services.runs.call(project_id, "fs.write", params)
    await services.runs.call(project_id, "git.stage", {"paths": sorted(files)})
    author, email = committer(user)
    commit = {"message": MESSAGE, "name": author, "email": email}
    await services.runs.call(project_id, "git.commit", commit)


def app_port(manifest_text: str) -> int | None:
    """The port of the product's web client (the service routed at /), from forge.app.toml."""
    manifest = parse_manifest(manifest_text)
    if isinstance(manifest, list):
        return None
    web = next((s for s in manifest.services if s.route == "/"), None)
    return web.port if web else None
