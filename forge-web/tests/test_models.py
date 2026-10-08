"""The model list the chat header offers, and the file search behind @-mentions."""

from pathlib import Path
from typing import Any

from forge_web.gateway.api import model_provider
from forge_web.settings import load_settings
from support import LiveServer, dev_settings, person


def docker_settings(data_dir: Path) -> Any:
    return load_settings(
        data_dir / "forge-web.toml",
        environ={"FORGE_WEB_DATA_DIR": str(data_dir)},
        overrides={"sandbox.isolation": "docker"},
    )


def test_models_belong_to_their_vendor() -> None:
    assert model_provider("claude-sonnet-5-5") == "anthropic"
    assert model_provider("gpt-5-mini") == "openai"
    assert model_provider("gemini-2.5-pro") == "google"
    assert model_provider("deepseek-chat") == "deepseek"
    assert model_provider("llama-3.3-70b-versatile") == "groq"
    assert model_provider("something-else") is None


async def test_models_follow_the_keys_a_user_may_use(tmp_path: Path) -> None:
    with LiveServer(docker_settings(tmp_path / "data")) as server:
        admin = await person(server, "admin", role="admin")
        ada = await person(server, "ada")
        assert (await ada.web.get("/api/models")).json() == []
        own = {"provider": "anthropic", "key": "sk-ant-0123456789"}
        assert (await ada.web.post("/api/keys", own)).status_code == 201
        models = (await ada.web.get("/api/models")).json()
        assert {m["provider"] for m in models} == {"anthropic"}
        sonnet = next(m for m in models if m["model"] == "claude-sonnet-5-5")
        assert sonnet["id"] == "anthropic/claude-sonnet-5-5" and sonnet["key"] == "own"
        assert sonnet["cost_in"] > 0 and sonnet["context_window"] >= 200_000
        server_key = {"provider": "openai", "key": "sk-0123456789"}
        assert (await admin.web.post("/api/admin/keys", server_key)).status_code == 201
        grant = await admin.web.request("PUT", f"/api/admin/grants/{ada.id}", json={})
        assert grant.status_code == 200
        providers = {m["provider"]: m["key"] for m in (await ada.web.get("/api/models")).json()}
        assert providers == {"anthropic": "own", "openai": "server"}


async def test_file_search_for_mentions(tmp_path: Path) -> None:
    with LiveServer(dev_settings(tmp_path / "data")) as server:
        owner = await person(server, "owner")
        created = await owner.web.post("/api/projects", {"name": "App"})
        project_id = created.json()["id"]
        workspace = server.services.driver.workspace(project_id)
        (workspace / "src").mkdir()
        (workspace / "src" / "main.py").write_text("print(1)\n")
        (workspace / "notes.md").write_text("x\n")
        found = await owner.web.get(f"/api/projects/{project_id}/files/search?query=main")
        assert found.status_code == 200 and found.json()["files"] == ["src/main.py"]
        stranger = await person(server, "stranger")
        refused = await stranger.web.get(f"/api/projects/{project_id}/files/search?query=main")
        assert refused.status_code == 404
