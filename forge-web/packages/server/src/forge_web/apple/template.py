"""A new Apple app project starts from Forge's SwiftUI template (`forge apple new`).

The files are made on the server from the template (no project code runs here), written into
the project's sandbox by its daemon and committed, so the project starts clean.
"""

import base64
import re

from fastapi import HTTPException
from forge.apple_template import app_files, bundle_problem, name_problem

from forge_web.db.models import User
from forge_web.git_api import committer
from forge_web.services import Services

GERMAN = {"ä": "ae", "ö": "oe", "ü": "ue", "Ä": "Ae", "Ö": "Oe", "Ü": "Ue", "ß": "ss"}
MAX_NAME = 30
MESSAGE = "Start from Forge's Apple app template"


def app_name(project_name: str) -> str:
    """A Swift name for the app from the project's name: letters and digits, starting with a
    letter ("Mein Zähler" -> "MeinZaehler", "2048" -> "App2048")."""
    spelled = "".join(GERMAN.get(c, c) for c in project_name)
    kept = re.sub(r"[^A-Za-z0-9]", "", spelled)
    if not kept or not kept[0].isalpha():
        kept = "App" + kept
    return kept[:MAX_NAME]


def checked_bundle_id(name: str, bundle_id: str) -> str:
    """The bundle id to use (com.example.<name> when none is given); 422 when it is invalid."""
    chosen = bundle_id.strip() or f"com.example.{name.lower()}"
    problem = bundle_problem(chosen) or name_problem(name)
    if problem:
        raise HTTPException(422, f"bundle id {chosen!r}: {problem}")
    return chosen


async def write_template(
    services: Services, project_id: str, name: str, bundle_id: str, user: User
) -> None:
    """Write the template's files into the project's sandbox and commit them as the user."""
    files = app_files(name, bundle_id)
    for path, data in files.items():
        params = {"path": path, "base64": base64.b64encode(data).decode(), "create_dirs": True}
        await services.runs.call(project_id, "fs.write", params)
    await services.runs.call(project_id, "git.stage", {"paths": sorted(files)})
    author, email = committer(user)
    commit = {"message": MESSAGE, "name": author, "email": email}
    await services.runs.call(project_id, "git.commit", commit)
