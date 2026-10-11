"""Full-stack app projects (W24): they start from Forge's template, their chats run with the
release review and the go-live question, and the preview knows the web client's port."""

import os
import shutil
import subprocess
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from forge.app_template import write_app

from forge_sandbox.methods import ChatOptions
from forge_sandbox.worker import ROLES, build_config, config_overrides
from forge_web.apps.template import app_port, product_name, product_title
from forge_web.db.models import Project
from forge_web.startup import chat_options
from support import LiveServer, dev_settings

IMAGE = os.environ.get("FORGE_WEB_TEST_IMAGE", "forge-web-sandbox:dev")


@pytest.fixture
def server(tmp_path: Path) -> Iterator[LiveServer]:
    with LiveServer(dev_settings(tmp_path / "data")) as live:
        yield live


@pytest.fixture
async def client(server: LiveServer) -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(base_url=server.url, headers=server.headers(), timeout=60) as c:
        yield c


def git_log(workspace: Path) -> str:
    """The workspace's commit messages."""
    found = subprocess.run(
        ["git", "-C", str(workspace), "log", "--format=%s"],
        capture_output=True,
        text=True,
        check=True,
    )
    return found.stdout


async def test_an_app_project_starts_from_the_template(
    server: LiveServer, client: httpx.AsyncClient
) -> None:
    created = await client.post("/api/projects", json={"name": "Mein Zähler", "source": "app"})
    assert created.status_code == 201, created.text
    project = created.json()
    assert project["kind"] == "app" and project["source"] == "app"
    workspace = server.services.driver.workspace(project["id"])
    manifest = (workspace / "forge.app.toml").read_text(encoding="utf-8")
    assert 'name = "mein-zaehler"' in manifest
    assert (workspace / "README.md").read_text(encoding="utf-8").startswith("# Mein Zaehler\n")
    assert (workspace / "server" / "app" / "main.py").is_file()
    assert (workspace / "web" / "src" / "App.tsx").is_file()
    assert (workspace / ".github" / "workflows" / "ci.yml").is_file()
    assert "Start from Forge's full-stack app template" in git_log(workspace)
    async with server.services.db.session() as session:
        row = await session.get(Project, project["id"])
    assert row is not None and row.kind == "app"
    assert server.services.runs.link_info[project["id"]]["app"] is True
    assert [p["kind"] for p in (await client.get("/api/projects")).json()] == ["app"]


def test_product_names_and_titles_come_from_project_names() -> None:
    assert product_name("Tally") == "tally"
    assert product_name("Mein Zähler!") == "mein-zaehler"
    assert product_name("2048") == "app-2048"
    assert product_name("Straße & Grüße") == "strasse-gruesse"
    assert product_name("😀") == "app"
    assert len(product_name("x" * 80)) == 40
    assert product_title("Mein Zähler!") == "Mein Zaehler"
    assert product_title("Shop <script>") == "Shop script"
    assert product_title("😀") == "App"
    assert len(product_title("y" * 90)) == 60


def test_app_chats_are_reviewed_before_they_may_go_live(tmp_path: Path) -> None:
    options_for = chat_options(dev_settings(tmp_path))
    chat = SimpleNamespace(mode="edits", model="")
    assert options_for(chat, {"gateway_port": 47101, "app": True})["app_review"] is True
    assert "app_review" not in options_for(chat, {"gateway_port": 47101, "app": False})
    overrides = config_overrides(ChatOptions(app_review=True, model="openai/gpt-5"))
    assert overrides["app.review"] is True
    assert overrides["roles"]["release_reviewer"] == ["openai/gpt-5"]
    assert {"release_reviewer", "architect"} <= set(ROLES)
    root = tmp_path / "product"
    root.mkdir()
    assert build_config(root, ChatOptions(app_review=True)).app.review
    assert not build_config(root, ChatOptions()).app.review


async def test_the_preview_knows_the_app_port_and_how_to_start_it(
    server: LiveServer, client: httpx.AsyncClient
) -> None:
    project = (await client.post("/api/projects", json={"name": "Shop", "source": "app"})).json()
    overview = (await client.get(f"/api/projects/{project['id']}/preview")).json()
    assert overview["app_port"] == 8080
    first = overview["suggestions"][0]
    assert first["label"] == "Forge app" and first["command"].endswith(" -m forge app dev")
    plain = (await client.post("/api/projects", json={"name": "Plain"})).json()
    assert (await client.get(f"/api/projects/{plain['id']}/preview")).json()["app_port"] is None


def test_the_app_port_comes_from_the_manifest() -> None:
    web = '[[services]]\nname = "web"\nruntime = "static"\nport = 3000\nroute = "/"\n'
    assert app_port(f'name = "shop"\n{web}') == 3000
    assert (
        app_port(
            'name = "shop"\n[[services]]\nname = "api"\nruntime = "node22"\n'
            'command = ["x"]\nport = 9000\n'
        )
        is None
    )
    assert app_port("not toml [") is None


def docker_ready() -> bool:
    """True when Docker runs and the sandbox image is built."""
    if shutil.which("docker") is None:
        return False
    found = subprocess.run(["docker", "image", "inspect", IMAGE], capture_output=True, timeout=30)
    return found.returncode == 0


@pytest.mark.docker
@pytest.mark.skipif(not docker_ready(), reason=f"needs Docker and the image {IMAGE}")
def test_forge_app_check_passes_inside_the_sandbox_image(tmp_path: Path) -> None:
    product = tmp_path / "shop"
    write_app(product, "shop")
    os.chmod(tmp_path, 0o777)
    for folder, _, files in os.walk(product):  # the image's user (uid 1000) works in it
        os.chmod(folder, 0o777)
        for name in files:
            os.chmod(Path(folder) / name, 0o666)
    command = ["docker", "run", "--rm", "--user", "1000:1000", "-v", f"{product}:/workspace"]
    ca = os.environ.get("FORGE_WEB_TEST_CA", "")  # behind a TLS-inspecting proxy: its CA
    if ca:
        command += ["-v", f"{ca}:/ca.crt:ro", "-e", "SSL_CERT_FILE=/ca.crt"]
        command += ["-e", "UV_SYSTEM_CERTS=1", "-e", "NODE_EXTRA_CA_CERTS=/ca.crt"]
        command += ["-e", "npm_config_cafile=/ca.crt"]
    command += ["--entrypoint", "/opt/forge/bin/forge", IMAGE, "-C", "/workspace", "app", "check"]
    result = subprocess.run(command, capture_output=True, text=True, timeout=1800)
    assert result.returncode == 0, result.stdout[-4000:] + result.stderr[-2000:]
    assert "app check: passed" in result.stdout
    assert "PASSED  server-tests (api)" in result.stdout and "PASSED  web (web)" in result.stdout
