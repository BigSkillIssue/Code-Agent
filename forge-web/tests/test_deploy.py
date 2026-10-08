"""Deployment files: the compose file, Caddy, the service templates and the server image fit the
server they run."""

import json
import os
import plistlib
import shutil
import subprocess
import tomllib
import xml.etree.ElementTree as ElementTree
from pathlib import Path

import pytest

from forge_web.app import create_app
from forge_web.settings import load_settings

ROOT = Path(__file__).parents[1]
DEPLOY = ROOT / "deploy"
ENV = {"FORGE_DOMAIN": "forge.example.com", "PREVIEW_DOMAIN": "preview.example.net",
       "ACME_EMAIL": "admin@example.com", "DOCKER_GID": "999"}  # fmt: skip


def compose_available() -> bool:
    if shutil.which("docker") is None:
        return False
    found = subprocess.run(["docker", "compose", "version"], capture_output=True, timeout=30)
    return found.returncode == 0


needs_compose = pytest.mark.skipif(not compose_available(), reason="needs docker compose")


def compose_config(env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    clean = {k: v for k, v in os.environ.items() if k not in ENV}
    return subprocess.run(
        ["docker", "compose", "-f", str(ROOT / "compose.yaml"), "config", "--format", "json"],
        capture_output=True, text=True, timeout=60, env={**clean, **env}, cwd=ROOT,
    )  # fmt: skip


@needs_compose
def test_the_compose_file_wires_server_sandbox_image_and_caddy() -> None:
    done = compose_config(ENV)
    assert done.returncode == 0, done.stderr
    services = json.loads(done.stdout)["services"]
    server = services["forge-web"]
    env = server["environment"]
    assert env["FORGE_WEB_SERVER__PUBLIC_URL"] == "https://forge.example.com"
    assert env["FORGE_WEB_PREVIEW__DOMAIN"] == "preview.example.net"
    assert env["FORWARDED_ALLOW_IPS"] == "*"
    assert server["group_add"] == ["999"]
    targets = {v["target"] for v in server["volumes"]}
    assert {"/data", "/var/run/docker.sock", "/config/forge-web.toml"} <= targets
    assert "ports" not in server  # only Caddy is reachable from outside
    assert services["sandbox-image"]["image"].endswith("forge-web-sandbox:latest")
    assert {p["target"] for p in services["caddy"]["ports"]} == {80, 443}
    missing = compose_config({k: v for k, v in ENV.items() if k != "DOCKER_GID"})
    assert missing.returncode != 0 and "DOCKER_GID" in missing.stderr


def test_caddy_asks_the_route_that_exists(tmp_path: Path) -> None:
    caddyfile = (DEPLOY / "Caddyfile").read_text()
    settings = load_settings(tmp_path / "forge-web.toml", environ={},
                             overrides={"data_dir": str(tmp_path)})  # fmt: skip
    paths = set(create_app(settings).openapi()["paths"])
    ask = caddyfile.split("ask http://", 1)[1].split()[0]
    assert "/" + ask.split("/", 1)[1] in paths  # /api/preview/allowed-host
    assert "on_demand" in caddyfile and "*.{$PREVIEW_DOMAIN}" in caddyfile


def test_the_service_templates_start_the_server() -> None:
    service: dict[str, list[str]] = {}  # systemd repeats keys, so no configparser
    for line in (DEPLOY / "forge-web.service").read_text().splitlines():
        if "=" in line and not line.startswith("#"):
            key, value = line.split("=", 1)
            service.setdefault(key, []).append(value)
    assert service["ExecStart"][0].endswith("forge-web serve")
    data = next(v.split("=", 1)[1] for v in service["Environment"]
                if v.startswith("FORGE_WEB_DATA_DIR="))  # fmt: skip
    assert service["ReadWritePaths"] == [data] and service["NoNewPrivileges"] == ["true"]
    plist = plistlib.loads((DEPLOY / "com.forge.web.plist").read_bytes())
    assert plist["ProgramArguments"][-1] == "serve" and plist["KeepAlive"] is True
    winsw = ElementTree.parse(DEPLOY / "forge-web-winsw.xml").getroot()
    assert winsw.findtext("arguments") == "serve"
    assert winsw.findtext("executable", "").endswith("forge-web.exe")


def test_the_server_image_builds_the_ui_and_runs_as_its_own_user() -> None:
    dockerfile = (ROOT / "docker" / "server.Dockerfile").read_text()
    assert "npm run build" in dockerfile and "forge_web/static" in dockerfile
    assert "USER forge-web" in dockerfile and 'CMD ["forge-web", "serve"]' in dockerfile
    ignored = (ROOT / "docker" / "server.Dockerfile.dockerignore").read_text().splitlines()
    assert "forge-web/frontend/node_modules" in ignored
    example = (DEPLOY / "forge-web.example.toml").read_text()
    assert tomllib.loads(example)["auth"]["admin_two_factor"] is True
