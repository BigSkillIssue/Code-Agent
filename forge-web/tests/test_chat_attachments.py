"""Effort and files in the chat (W45): the chat's effort reaches Forge, an editor uploads files
of up to 20 MB into the workspace, and a message's files reach the model (pictures as images)."""

import base64
from pathlib import Path
from typing import Any

import httpx
import pytest
from forge.providers.base import ImagePart
from forge.providers.fake import FakeProvider
from forge.runtime.attachments import Attachment

from forge_sandbox.methods import ChatOptions
from forge_sandbox.worker import build_config, config_overrides
from forge_web.chats import attachments as uploads
from support import LiveServer, dev_settings, fake_script, person
from test_hosting_deploy import until
from test_worker import home, root, started

__all__ = ["home", "root"]  # fixtures

PIXEL = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg=="
)


def test_the_chats_effort_becomes_forges(root: Path, home: Path) -> None:
    assert "effort" not in config_overrides(ChatOptions())  # auto: the model's own default
    assert config_overrides(ChatOptions(effort="high"))["effort"] == "high"
    assert build_config(root, ChatOptions(effort="low")).effort == "low"


async def test_an_attached_picture_reaches_the_model_as_an_image(root: Path, home: Path) -> None:
    (root / ".forge" / "attachments").mkdir(parents=True)
    (root / ".forge" / "attachments" / "20261018-ab12cd34-shot.png").write_bytes(PIXEL)
    worker, out = await started(root, ChatOptions(fake_script=fake_script({"text": "A dot."})))
    shot = Attachment(path=".forge/attachments/20261018-ab12cd34-shot.png", name="shot.png",
                      media_type="image/png", size=len(PIXEL))  # fmt: skip
    assert worker.submit("What is on it?", [shot])
    turn = await out.wait_for("turn")
    assert turn["ok"], turn
    assert worker.ctx is not None
    [fake] = [p for p in worker.ctx.cfg.instances.values() if isinstance(p, FakeProvider)]
    images = [
        p for r in fake.requests for m in r.messages for p in m.parts if isinstance(p, ImagePart)
    ]
    assert images and base64.b64decode(images[0].data_b64) == PIXEL
    assert "shot.png" in fake.requests[0].messages[0].text()
    await worker.close()


@pytest.fixture
def server(tmp_path: Path) -> Any:
    script = fake_script({"text": "Got the picture."})
    with LiveServer(dev_settings(tmp_path / "data", script)) as live:
        yield live


async def project_chat(client: httpx.AsyncClient) -> tuple[str, str]:
    project = (await client.post("/api/projects", json={"name": "Demo"})).json()
    chat = (await client.get(f"/api/projects/{project['id']}/chat")).json()
    return project["id"], chat["id"]


async def test_an_editor_uploads_a_file_and_sends_it_with_a_message(server: LiveServer) -> None:
    async with httpx.AsyncClient(
        base_url=server.url, headers=server.headers(), timeout=60
    ) as client:
        project_id, chat_id = await project_chat(client)
        named = {"name": "Screen shot.png"}
        made = await client.post(f"/api/chats/{chat_id}/attachments", params=named,
                                 content=PIXEL, headers={"Content-Type": "image/png"})  # fmt: skip
        assert made.status_code == 201, made.text
        file = made.json()
        assert file["path"].startswith(".forge/attachments/") and file["path"].endswith(
            "-Screen_shot.png"
        )
        assert (file["name"], file["media_type"], file["size"]) == (
            "Screen shot.png",
            "image/png",
            len(PIXEL),
        )
        workspace = server.services.driver.workspace(project_id)
        assert (workspace / file["path"]).read_bytes() == PIXEL
        message = {"text": "What is on it?", "attachments": [file]}
        sent = await client.post(f"/api/chats/{chat_id}/messages", json=message)
        assert sent.status_code == 202, sent.text

        async def finished() -> list[dict[str, Any]]:
            items = (await client.get(f"/api/chats/{chat_id}/events")).json()["items"]
            return items if any(i["item"]["type"] == "turn" for i in items) else []

        items = await until(finished)
        [user] = [i["item"] for i in items if i["item"]["type"] == "user"]
        assert user["attachments"] == [file]
        [turn] = [i["item"] for i in items if i["item"]["type"] == "turn"]
        assert turn["ok"], turn
        outside = {"text": "x", "attachments": [{**file, "path": "../.env"}]}
        bad = await client.post(f"/api/chats/{chat_id}/messages", json=outside)
        assert bad.status_code == 422
        effort = await client.patch(f"/api/chats/{chat_id}", json={"effort": "high"})
        assert effort.json()["effort"] == "high"
        assert (
            await client.patch(f"/api/chats/{chat_id}", json={"effort": "max"})
        ).status_code == 422


async def test_uploads_have_a_size_limit_need_room_and_an_editor(
    server: LiveServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with httpx.AsyncClient(
        base_url=server.url, headers=server.headers(), timeout=60
    ) as client:
        project_id, chat_id = await project_chat(client)
        url = f"/api/chats/{chat_id}/attachments"
        monkeypatch.setattr(uploads, "MAX_MB", 1)
        too_big = await client.post(
            url, params={"name": "big.bin"}, content=b"x" * (1024 * 1024 + 1)
        )
        assert too_big.status_code == 413
        workspace = server.services.driver.workspace(project_id)
        assert not list((workspace / ".forge" / "attachments").glob("*big.bin"))

        async def full(_services: Any, _project_id: str) -> int:
            return 0

        monkeypatch.setattr(uploads, "disk_room", full)
        assert (await client.post(url, params={"name": "a.txt"}, content=b"a")).status_code == 413
    viewer = await person(server, "viewer")
    from forge_web.db.models import ProjectMember

    async with server.services.db.session() as session, session.begin():
        session.add(ProjectMember(project_id=project_id, user_id=viewer.id, role="viewer"))
    refused = await viewer.web.request("POST", url, params={"name": "a.txt"}, content=b"a")
    assert refused.status_code == 403
    await viewer.web.client.aclose()
