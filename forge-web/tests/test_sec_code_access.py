"""SEC-W3: without code access (W57) the preview runs only the server's own suggestions, and
nobody without the code reads a program's output or the command lines others typed."""

from pathlib import Path
from typing import Any

import pytest

from forge_web.db.models import ProjectMember
from support import LiveServer, dev_settings, person

POSIX_ONLY = pytest.mark.skipif(__import__("sys").platform == "win32", reason="bash programs")
MANAGE = "print('SECRET_FROM_THE_CODE')\n"  # what `python3 manage.py runserver` prints here


@pytest.fixture
def server(tmp_path: Path) -> Any:
    settings = dev_settings(tmp_path / "data")
    settings.agents.code_access_without_plan = False  # people without a plan: no code
    with LiveServer(settings) as live:
        yield live


def workspace(server: LiveServer, project_id: str) -> Path:
    """The project's files on disk (local isolation)."""
    folder = server.services.settings.data_dir / "projects" / project_id / "workspace"
    folder.mkdir(parents=True, exist_ok=True)
    return folder


@POSIX_ONLY
async def test_preview_programs_follow_code_access(server: LiveServer) -> None:
    maria = await person(server, "maria")
    pid = (await maria.web.post("/api/projects", {"name": "Lehm"})).json()["id"]
    root = workspace(server, pid)
    (root / "manage.py").write_text(MANAGE)
    (root / "index.html").write_text("<h1>hi</h1>")
    base = f"/api/projects/{pid}/preview"

    free = await maria.web.post(f"{base}/programs", {"command": "cat $(git ls-files)"})
    assert free.status_code == 403, free.text
    static = await maria.web.post(
        f"{base}/programs", {"command": "python3 -m http.server 8000 --bind 127.0.0.1"}
    )
    assert static.status_code == 403, static.text
    elsewhere = await maria.web.post(f"{base}/programs", {
        "command": "python3 manage.py runserver 127.0.0.1:8000", "cwd": ".git"})  # fmt: skip
    assert elsewhere.status_code == 403, elsewhere.text

    suggested = await maria.web.post(
        f"{base}/programs", {"command": "python3 manage.py runserver 127.0.0.1:8000"}
    )
    assert suggested.status_code == 201, suggested.text
    program = suggested.json()["id"]
    output = await maria.web.get(f"{base}/programs/{program}/output")
    assert output.status_code == 403, output.text
    assert "SECRET_FROM_THE_CODE" not in output.text
    assert (await maria.web.request("DELETE", f"{base}/programs/{program}")).status_code == 200

    overview = await maria.web.get(base)
    assert overview.status_code == 200, overview.text
    shown = overview.json()
    assert shown["code_access"] is False
    commands = [s["command"] for s in shown["suggestions"]]
    assert "python3 manage.py runserver 127.0.0.1:8000" in commands
    # a static file server would hand out every file of the project, .git included
    assert not any("http.server" in c for c in commands)


@POSIX_ONLY
async def test_people_with_the_code_keep_free_commands(server: LiveServer) -> None:
    maria = await person(server, "maria")
    pid = (await maria.web.post("/api/projects", {"name": "Lehm"})).json()["id"]
    ada = await person(server, "ada", role="admin")  # admins always see the code
    async with server.services.db.session() as session, session.begin():
        session.add(ProjectMember(project_id=pid, user_id=ada.id, role="editor"))
    base = f"/api/projects/{pid}/preview"
    started = await ada.web.post(
        f"{base}/programs", {"command": "python3 -c \"print('SECRET_FREE')\""}
    )
    assert started.status_code == 201, started.text
    program = started.json()["id"]
    assert (await ada.web.get(f"{base}/programs/{program}/output")).status_code == 200
    assert (await ada.web.get(base)).json()["code_access"] is True

    # maria sees that something runs, but not the command line someone else typed
    shown = (await maria.web.get(base)).json()
    listed = next(p for p in shown["programs"] if p["id"] == program)
    assert "SECRET_FREE" not in str(listed)
    await ada.web.request("DELETE", f"{base}/programs/{program}")
