"""Code access by plan (W57): without it a person gets no files, git, terminal or export, the chat
arrives without code, and turns say only how many lines changed."""

import subprocess
from pathlib import Path
from typing import Any

import pytest

from forge_sandbox.methods import ChatOptions
from forge_sandbox.worker import lines_changed
from forge_web.code_access import prose, redacted_item, redacted_message
from support import LiveServer, dev_settings, person

EDIT = {"type": "event", "event": {
    "kind": "tool_started", "session_id": "s", "ts": 1.0,
    "call": {"id": "c1", "name": "edit_file",
             "arguments": {"path": "server/app/main.py", "old": "x = 1", "new": "SECRET = 'k'"}},
}}  # fmt: skip
READ = {"type": "event", "event": {
    "kind": "tool_finished", "session_id": "s", "ts": 1.0,
    "result": {"call_id": "c1", "ok": True, "text": "def secret_sauce(): ...", "images": []},
}}  # fmt: skip
FASTAPI_ANSWER = "Done. It uses FastAPI:\n```python\nAPI_KEY = 'x'\n```\nAll good."
ANSWER = {"type": "event", "event": {
    "kind": "model_done", "session_id": "s", "ts": 1.0, "usage": {},
    "message": {"role": "assistant", "parts": [
        {"type": "text", "text": FASTAPI_ANSWER}],
        "tool_calls": [{"id": "c2", "name": "write_file",
                        "arguments": {"path": "a.py", "content": "print(1)"}}]},
}}  # fmt: skip


def test_items_lose_the_code_but_keep_what_happened() -> None:
    edit = redacted_item(EDIT)
    assert edit is not None
    assert edit["event"]["call"]["arguments"] == {"path": "server/app/main.py"}
    read = redacted_item(READ)
    assert read is not None and "secret_sauce" not in str(read)
    answer = redacted_item(ANSWER)
    assert answer is not None
    text = answer["event"]["message"]["parts"][0]["text"]
    assert "FastAPI" in text and "API_KEY" not in text and "[code hidden]" in text
    assert answer["event"]["message"]["tool_calls"][0]["arguments"] == {"path": "a.py"}
    assert redacted_item({"type": "event", "event": {"kind": "tool_output", "text": "x"}}) is None
    assert redacted_message({"type": "live", "chat_id": "c", "item": {}}) is None
    snapshot = redacted_message({"type": "subscribed", "chat_id": "c", "live": {
        "streaming": {"main": "def x"}, "outputs": {"c1": ["secret"]}, "pending": []}})  # fmt: skip
    assert snapshot is not None and snapshot["live"]["streaming"] == {}
    assert prose("    a = 1\n    b = 2\n    c = 3\n    d = 4\nend") == "[code hidden]\nend"


def git(root: Path, *args: str) -> None:
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@x.y", *args], cwd=root,
                   check=True, capture_output=True)  # fmt: skip


def test_a_turn_counts_the_lines_it_changed(tmp_path: Path) -> None:
    git(tmp_path, "init", "-q")
    (tmp_path / "a.py").write_text("one\ntwo\nthree\n")
    git(tmp_path, "add", "-A")
    git(tmp_path, "commit", "-q", "-m", "start")
    base = subprocess.run(["git", "rev-parse", "HEAD"], cwd=tmp_path, capture_output=True,
                          text=True, check=True).stdout.strip()  # fmt: skip
    (tmp_path / "a.py").write_text("one\nTWO\n")
    (tmp_path / "b.py").write_text("new\nfile\n")
    assert lines_changed(tmp_path, base) == (1 + 2, 2)
    assert ChatOptions(code_private=True).code_private


@pytest.fixture
def server(tmp_path: Path) -> Any:
    settings = dev_settings(tmp_path / "data")
    settings.agents.code_access_without_plan = False  # people without a plan: no code
    with LiveServer(settings) as live:
        yield live


async def test_without_code_access_the_code_stays_on_the_server(server: LiveServer) -> None:
    maria = await person(server, "maria")
    made = await maria.web.post("/api/projects", {"name": "Lehm"})
    assert made.status_code == 201, made.text
    project = made.json()
    assert project["code_access"] is False
    pid = project["id"]
    refused = [
        await maria.web.get(f"/api/projects/{pid}/files/list?path="),
        await maria.web.get(f"/api/projects/{pid}/files/content?path=README.md"),
        await maria.web.get(f"/api/projects/{pid}/files/raw?path=README.md"),
        await maria.web.get(f"/api/projects/{pid}/files/search?q=a"),
        await maria.web.get(f"/api/projects/{pid}/git/status"),
        await maria.web.get(f"/api/projects/{pid}/git/diff?path=README.md"),
        await maria.web.post(f"/api/projects/{pid}/git/push", {}),
        await maria.web.get(f"/api/projects/{pid}/terminals"),
    ]
    assert [r.status_code for r in refused] == [403] * len(refused), [r.text for r in refused]
    assert (await maria.web.get(f"/api/projects/{pid}/product")).status_code == 200
    listed = (await maria.web.get("/api/projects")).json()
    assert listed[0]["code_access"] is False
    admin = await person(server, "ada", role="admin")
    # an admin always may, and sees the project as a member would only if invited: here, theirs
    own = (await admin.web.post("/api/projects", {"name": "Mine"})).json()
    assert own["code_access"] is True
    files = await admin.web.get(f"/api/projects/{own['id']}/files/list?path=")
    assert files.status_code == 200
