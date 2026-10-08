"""The preview tab's API: what could run, what runs, where it listens, and opening a preview.

Programs are the sandbox's `procs`; ports are what programs in the sandbox listen on. Opening a
preview hands out a one-time ticket URL on the preview's own host (see `preview_auth`). Viewers
may look at previews; only editors start and stop programs.
"""

import json
import re
from typing import Any
from urllib.parse import urlencode

from fastapi import APIRouter, HTTPException, Path, Request
from pydantic import BaseModel, Field

from forge_web.auth.sessions import CurrentUser, session_token, token_id
from forge_web.files_api import allowed
from forge_web.preview_auth import ENTER_PATH, preview_base
from forge_web.sandbox_calls import result_dict, sandbox_call
from forge_web.services import Services

PROGRAM_ID = re.compile(r"^p[0-9a-f]{8}$")
NODE_SCRIPTS = ("dev", "start", "serve", "preview")
LOCK_FILES = (("pnpm-lock.yaml", "pnpm"), ("yarn.lock", "yarn"), ("bun.lockb", "bun"),
              ("bun.lock", "bun"))  # fmt: skip
NO_DOMAIN = "previews need a preview domain on this server ([preview] domain in the settings)"


class ProgramIn(BaseModel):
    """A command line to start in the project (a dev server, usually)."""

    command: str = Field(min_length=1, max_length=2000)
    cwd: str = Field(default="", max_length=1000)


def check_program_id(program_id: str) -> str:
    """A program id as the sandbox makes them; 404 otherwise."""
    if not PROGRAM_ID.match(program_id):
        raise HTTPException(404, "no such program")
    return program_id


def preview_router() -> APIRouter:
    """`/api/projects/{id}/preview/...`."""
    router = APIRouter(prefix="/api/projects/{project_id}/preview")

    @router.get("")
    async def overview(project_id: str, request: Request, user: CurrentUser) -> dict[str, Any]:
        services = await allowed(request, user, project_id)
        programs = await sandbox_call(services, project_id, "procs.list")
        local = services.settings.sandbox.isolation == "local"  # the host's ports are there too
        ports = await sandbox_call(
            services, project_id, "ports.list", {"owned": True} if local else {}
        )
        return {
            "enabled": preview_base(services.settings) is not None,
            "suggestions": await suggestions(services, project_id),
            "programs": objects(programs),
            "ports": listening(services, ports),
        }

    @router.post("/programs", status_code=201)
    async def start(project_id: str, body: ProgramIn, request: Request, user: CurrentUser) -> Any:
        services = await allowed(request, user, project_id, "editor")
        params = {"command": body.command, "cwd": body.cwd, "name": "preview"}
        return result_dict(await sandbox_call(services, project_id, "procs.start", params))

    @router.delete("/programs/{program_id}")
    async def stop(project_id: str, program_id: str, request: Request, user: CurrentUser) -> Any:
        services = await allowed(request, user, project_id, "editor")
        params = {"id": check_program_id(program_id)}
        return result_dict(await sandbox_call(services, project_id, "procs.stop", params))

    @router.get("/programs/{program_id}/output")
    async def output(project_id: str, program_id: str, request: Request, user: CurrentUser,
                     since: int = 0) -> Any:  # fmt: skip
        services = await allowed(request, user, project_id)
        params = {"id": check_program_id(program_id), "since": max(since, 0), "limit": 500}
        return result_dict(await sandbox_call(services, project_id, "procs.output", params))

    @router.post("/{port}/open")
    async def open_preview(project_id: str, request: Request, user: CurrentUser,
                           port: int = Path(ge=1, le=65535)) -> dict[str, str]:  # fmt: skip
        services = await allowed(request, user, project_id)
        base = preview_base(services.settings)
        if base is None:
            raise HTTPException(409, NO_DOMAIN)
        session_id = token_id(session_token(request.cookies) or "")
        ticket = services.previews.issue(user.id, session_id, project_id, port)
        query = urlencode({"ticket": ticket, "next": "/"})
        return {"url": base.url(project_id, port, f"{ENTER_PATH}?{query}")}

    return router


def objects(found: Any) -> list[dict[str, Any]]:
    """The objects in a list from the sandbox (anything else is dropped)."""
    return [item for item in found if isinstance(item, dict)] if isinstance(found, list) else []


def listening(services: Services, found: Any) -> list[dict[str, Any]]:
    """The sandbox's listening ports (checked; Forge's own port left out in local mode)."""
    settings = services.settings
    own = (
        {settings.server.port, settings.preview.port}
        if settings.sandbox.isolation == "local"
        else set()
    )
    ports = []
    for entry in found if isinstance(found, list) else []:
        port = entry.get("port") if isinstance(entry, dict) else None
        if isinstance(port, int) and 0 < port < 65536 and port not in own:
            ports.append({"port": port, "address": str(entry.get("address", ""))[:64]})
    return ports


async def read_text(services: Services, project_id: str, path: str) -> str:
    """A small text file of the project, or "" if it cannot be read."""
    try:
        found = await sandbox_call(services, project_id, "fs.read", {"path": path, "limit": 65536})
    except HTTPException:
        return ""
    text = found.get("text") if isinstance(found, dict) else None
    return text if isinstance(text, str) else ""


async def suggestions(services: Services, project_id: str) -> list[dict[str, str]]:
    """Commands that probably start the project's dev server, best first."""
    listing = result_dict(await sandbox_call(services, project_id, "fs.list", {"path": ""}))
    names = {e.get("name") for e in objects(listing.get("entries"))}
    found: list[tuple[str, str]] = []
    if "package.json" in names:
        found += node_commands(await read_text(services, project_id, "package.json"), names)
    if "manage.py" in names:
        found.append(("Django", "python3 manage.py runserver 127.0.0.1:8000"))
    for name in ("app.py", "main.py"):
        if name in names:
            text = await read_text(services, project_id, name)
            if "FastAPI(" in text:
                found.append(
                    ("FastAPI", f"python3 -m uvicorn {name[:-3]}:app --reload --port 8000")
                )
            elif "Flask(" in text:
                found.append(("Flask", f"python3 -m flask --app {name[:-3]} run --port 5000"))
    if "index.html" in names:
        found.append(("index.html", "python3 -m http.server 8000 --bind 127.0.0.1"))
    return [{"label": label, "command": command} for label, command in found]


def node_commands(package_json: str, names: set[Any]) -> list[tuple[str, str]]:
    """`npm run dev` and friends for the scripts package.json has (installing first if needed)."""
    try:
        scripts = json.loads(package_json).get("scripts", {})
    except (ValueError, AttributeError):
        return []
    if not isinstance(scripts, dict):
        return []
    tool = next((t for lock, t in LOCK_FILES if lock in names), "npm")
    install = "" if "node_modules" in names else f"{tool} install && "
    found = []
    for script in NODE_SCRIPTS:
        if isinstance(scripts.get(script), str):
            run = f"{tool} start" if script == "start" else f"{tool} run {script}"
            found.append((f"{script} (package.json)", install + run))
    return found
