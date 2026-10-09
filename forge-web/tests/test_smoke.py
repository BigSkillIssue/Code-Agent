"""Both packages import, the commands print their versions, and the server answers."""

import re
import subprocess
import sys
from pathlib import Path

import httpx

import forge_sandbox
import forge_web
from forge_web.app import create_app
from forge_web.settings import WebSettings


def test_versions() -> None:
    assert forge_web.__version__ == "0.1.0"
    assert forge_sandbox.__version__ == "0.1.0"


def test_forge_web_version_command() -> None:
    out = subprocess.run(
        [sys.executable, "-m", "forge_web", "--version"], capture_output=True, text=True, check=True
    )
    assert out.stdout.strip() == "forge-web 0.1.0"


def test_forge_sandbox_version_command() -> None:
    out = subprocess.run(
        [sys.executable, "-m", "forge_sandbox", "--version"],
        capture_output=True,
        text=True,
        check=True,
    )
    assert out.stdout.strip() == "forge-sandbox 0.1.0"


async def test_health(settings: WebSettings) -> None:
    transport = httpx.ASGITransport(app=create_app(settings))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/api/health")
    assert response.status_code == 200
    assert response.json() == {"ok": True, "version": "0.1.0"}


def test_the_server_imports_only_the_sandboxs_shared_modules() -> None:
    # AGENTS.md rule 2: never the daemon, its services or the worker.
    shared = {"frames", "protocol", "methods", "mux", "rpc", "streams", "fingerprint"}
    server = Path(__file__).parents[1] / "packages" / "server" / "src"
    pattern = re.compile(r"^(?:from|import) forge_sandbox\.(\w+)", re.MULTILINE)
    used = {name for f in server.rglob("*.py") for name in pattern.findall(f.read_text())}
    assert used and used <= shared, used - shared
