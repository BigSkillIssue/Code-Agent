"""Hosting in one command (W64): the node installer Forge Web serves, the wheel it installs, and
`deploy/install.sh` for Forge Web itself."""

import json
import re
import shlex
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI

from forge_web.app import create_app
from forge_web.hosting.install import install_command, node_script, render_node_script
from forge_web.hosting.install_api import install_router
from forge_web.settings import WebSettings

WEB = Path(__file__).resolve().parents[1]
INSTALL_SH = WEB / "deploy" / "install.sh"
BASE = "https://forge.example.com"
HOSTILE = [
    "https://forge.example.com/'; rm -rf / #",
    "https://$(id).example.com",
    "https://forge.example.com/`id`",
    "https://forge.example.com\nrm -rf /",
    "https://forge.example.com/a b",
    "https://user@forge.example.com",
    "https://forge.example.com/?next=x",
    "https://forge.example.com/#x",
    "http://forge.example.com",
    "ftp://forge.example.com",
    "forge.example.com",
    "",
]
needs_bash = pytest.mark.skipif(
    shutil.which("bash") is None or sys.platform == "win32", reason="needs bash (Linux, macOS)"
)


def with_url(settings: WebSettings, url: str, apps_domain: str = "") -> WebSettings:
    """These settings with Forge Web reachable at `url`."""
    server = settings.server.model_copy(update={"public_url": url})
    hosting = settings.hosting.model_copy(update={"apps_domain": apps_domain})
    return settings.model_copy(update={"server": server, "hosting": hosting})


def client(settings: WebSettings, wheels: Path) -> httpx.AsyncClient:
    """A client for the install routes alone, with these settings."""
    app = FastAPI()
    app.state.services = SimpleNamespace(settings=settings)
    app.include_router(install_router(wheels_dir=wheels))
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


def bash(*args: str, script: str | None = None) -> subprocess.CompletedProcess[str]:
    """Bash on these arguments (a script on stdin when given), never a login shell."""
    return subprocess.run(
        ["bash", *args], input=script, capture_output=True, text=True, timeout=60, check=False
    )


# -- the rendered node script ---------------------------------------------------------------


def test_rendered_script_carries_the_quoted_address() -> None:
    script = render_node_script(BASE + "/", apps_domain="apps.example.net")
    assert f"FORGE_SERVER={shlex.quote(BASE)}\n" in script
    assert f"APPS_DOMAIN_DEFAULT={shlex.quote('apps.example.net')}\n" in script
    assert "@@" not in script
    assert script.startswith("#!/usr/bin/env bash\n")
    assert "set -euo pipefail" in script


def test_a_path_below_the_address_is_kept_and_quoted() -> None:
    script = render_node_script("https://example.com:8443/forge")
    assert f"FORGE_SERVER={shlex.quote('https://example.com:8443/forge')}\n" in script


@pytest.mark.parametrize("url", HOSTILE)
def test_an_address_that_would_break_quoting_is_refused(url: str) -> None:
    with pytest.raises(ValueError, match="https"):
        render_node_script(url)


def test_a_hostile_apps_domain_is_refused() -> None:
    with pytest.raises(ValueError, match="domain"):
        render_node_script(BASE, apps_domain="x.example.net'; reboot")


def test_the_embedded_unit_and_daemon_settings_are_the_deploy_files() -> None:
    template = node_script()
    unit = (WEB / "deploy" / "host" / "forge-host-worker.service").read_text(encoding="utf-8")
    body = unit[unit.index("[Unit]") :]
    assert body in template
    daemon = re.search(r"<<'DAEMON'\n(.*?)\nDAEMON\n", template, re.S)
    assert daemon is not None
    assert json.loads(daemon.group(1)) == json.loads(
        (WEB / "deploy" / "host" / "daemon.json").read_text(encoding="utf-8")
    )


def test_the_install_command_for_the_admin_page() -> None:
    command = install_command(BASE, "fjn_abc-DEF_123", ["apps", "db"])
    assert command == (
        "curl -fsSL https://forge.example.com/install/node.sh"
        " | sudo bash -s -- --code fjn_abc-DEF_123 --roles apps,db"
    )
    with pytest.raises(ValueError):
        install_command(BASE, "fjn_x; reboot", ["apps"])
    with pytest.raises(ValueError):
        install_command(BASE, "fjn_abc", ["apps", "root"])
    with pytest.raises(ValueError):
        install_command("http://forge.example.com", "fjn_abc", ["apps"])


# -- bash on both scripts -------------------------------------------------------------------


@needs_bash
def test_both_scripts_parse(tmp_path: Path) -> None:
    node = tmp_path / "node.sh"
    node.write_text(render_node_script(BASE, apps_domain="apps.example.net"), encoding="utf-8")
    for path in (node, INSTALL_SH):
        result = bash("-n", str(path))
        assert result.returncode == 0, (path, result.stderr)


@needs_bash
def test_node_script_help_and_bad_options_stop_before_doing_anything() -> None:
    script = render_node_script(BASE)
    shown = bash("-s", "--", "--help", script=script)
    assert shown.returncode == 0
    assert "--code" in shown.stdout and "--dry-run" in shown.stdout
    for args in (
        ["--roles", "apps,root"],
        ["--code", "fjn_x;reboot"],
        ["--endpoint", "$(id):51820"],
        ["--apps-domain", "a b"],
        ["--what"],
        ["--code"],
    ):
        result = bash("-s", "--", *args, script=script)
        assert result.returncode != 0, args
        assert "forge node:" in result.stderr, (args, result.stderr)


@needs_bash
def test_install_sh_help_and_bad_options() -> None:
    shown = bash(str(INSTALL_SH), "--help")
    assert shown.returncode == 0
    assert "--domain" in shown.stdout and "--dry-run" in shown.stdout
    for args in (["--domain", "forge.example.com;reboot"], ["--email", "not-a-mail"], ["--x"]):
        result = bash(str(INSTALL_SH), *args)
        assert result.returncode != 0, args
        assert "forge-web install:" in result.stderr, (args, result.stderr)


@needs_bash
@pytest.mark.skipif(sys.platform != "linux", reason="Forge Web's installer is for Linux")
def test_install_sh_dry_run_changes_nothing() -> None:
    before = sorted(p.name for p in WEB.iterdir())
    result = bash(
        str(INSTALL_SH), "--dry-run", "--yes", "--domain", "forge.example.com",
        "--preview-domain", "preview.example.net", "--email", "admin@example.com",
    )  # fmt: skip
    assert result.returncode == 0, result.stderr
    assert "docker compose up -d" in result.stdout
    assert "forge-web doctor" in result.stdout
    assert sorted(p.name for p in WEB.iterdir()) == before


# -- the routes -----------------------------------------------------------------------------


async def test_node_sh_route_renders_the_address(settings: WebSettings, tmp_path: Path) -> None:
    async with client(with_url(settings, BASE, "apps.example.net"), tmp_path) as http:
        response = await http.get("/install/node.sh")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/x-shellscript")
    assert response.headers["cache-control"] == "no-store"
    assert f"FORGE_SERVER={BASE}\n" in response.text
    assert "APPS_DOMAIN_DEFAULT=apps.example.net\n" in response.text


@pytest.mark.parametrize("url", ["", "http://forge.example.com", "https://x.example.com/'$(id)"])
async def test_node_sh_route_refuses_an_address_it_cannot_quote(
    settings: WebSettings, tmp_path: Path, url: str
) -> None:
    async with client(with_url(settings, url), tmp_path) as http:
        response = await http.get("/install/node.sh")
    assert response.status_code == 500
    assert "public_url" in response.json()["detail"]
    assert "#!/" not in response.text


async def test_wheel_route_404_without_a_wheel(settings: WebSettings, tmp_path: Path) -> None:
    (tmp_path / "forge_web-0.1.0-py3-none-any.whl").write_bytes(b"not this one")
    async with client(with_url(settings, BASE), tmp_path) as http:
        response = await http.get("/install/forge_hostworker.whl")
    assert response.status_code == 404
    async with client(with_url(settings, BASE), tmp_path / "missing") as http:
        assert (await http.get("/install/forge_hostworker.whl")).status_code == 404


async def test_wheel_route_serves_the_wheel(settings: WebSettings, tmp_path: Path) -> None:
    name = "forge_hostworker-0.1.0-py3-none-any.whl"
    (tmp_path / name).write_bytes(b"PK\x03\x04 a wheel")
    async with client(with_url(settings, BASE), tmp_path) as http:
        response = await http.get("/install/forge_hostworker.whl")
    assert response.status_code == 200
    assert response.content == b"PK\x03\x04 a wheel"
    # pip needs the real file name; the script keeps it with `curl -J`.
    assert f'filename="{name}"' in response.headers["content-disposition"]


async def test_the_app_mounts_the_install_routes(settings: WebSettings) -> None:
    transport = httpx.ASGITransport(app=create_app(settings))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
        response = await http.get("/install/forge_hostworker.whl")
    # The route's own answer (no wheel outside the image), not the web UI's page.
    assert response.status_code == 404
    assert "host worker wheel" in response.json()["detail"]


# -- the real thing, dry -----------------------------------------------------------------


@pytest.mark.docker
def test_node_script_dry_run_on_ubuntu_24_04() -> None:
    docker = shutil.which("docker") or pytest.skip("needs docker")
    script = render_node_script(BASE, apps_domain="apps.example.net")
    options = ["--code", "fjn_abcdefghijklmnop", "--roles", "apps,ai"]
    options += ["--endpoint", "198.51.100.7:51820"]
    result = subprocess.run(
        [docker, "run", "--rm", "-i", "ubuntu:24.04", "bash", "-s", "--", "--dry-run", *options],
        input=script, capture_output=True, text=True, timeout=300, check=False,
    )  # fmt: skip
    assert result.returncode == 0, result.stderr
    out = result.stdout
    for expected in (
        "get.docker.com",
        "wireguard-tools",
        "runsc.sha512",
        "/etc/docker/daemon.json",
        "caddy-api",
        "nvidia-smi",
        "/opt/forge-host",
        f"{BASE}/install/forge_hostworker.whl",
        "forge-host-worker join --server https://forge.example.com --code fjn_abcdefghijklmnop"
        " --data /var/lib/forge-host --endpoint 198.51.100.7:51820",
        "Environment=APPS_DOMAIN=apps.example.net",
        "systemctl enable forge-host-worker",
        "forge-host-worker doctor --data /var/lib/forge-host",
    ):
        assert expected in out, expected
    assert "WARNING" in result.stderr  # no NVIDIA driver in a plain container
