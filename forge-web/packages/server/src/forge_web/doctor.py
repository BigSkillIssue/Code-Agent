"""`forge-web doctor`: checks what a working server needs and says how to fix what is missing.

It reads the settings like `serve` does and looks at the machine: Docker and the sandbox image
(and whether it holds this server's Forge), gVisor, the event loop, the address people use,
sign-in providers, previews and the web UI. Nothing is changed.
"""

import asyncio
import shutil
import socket
import sys
import tempfile
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from forge_sandbox.fingerprint import forge_fingerprint
from forge_web.auth.oauth_providers import load_providers
from forge_web.preview_auth import LOOPBACK, preview_base
from forge_web.settings import WebSettings
from forge_web.webui import STATIC

Level = Literal["ok", "warn", "error"]
Run = Callable[[list[str]], Awaitable[tuple[int, str]]]
Resolve = Callable[[str], Awaitable[bool]]
MARKS = {"ok": "OK  ", "warn": "WARN", "error": "FAIL"}


@dataclass
class Finding:
    """One check's result."""

    level: Level
    title: str
    detail: str = ""
    fix: str = ""


async def run_command(argv: list[str], timeout: float = 30) -> tuple[int, str]:
    """Exit code and output of a command (127 if it cannot be started)."""
    try:
        process = await asyncio.create_subprocess_exec(
            *argv, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT
        )
    except OSError as err:
        return 127, str(err)
    try:
        out, _ = await asyncio.wait_for(process.communicate(), timeout)
    except TimeoutError:
        process.kill()
        return 124, "timed out"
    return process.returncode or 0, out.decode("utf-8", "replace").strip()


async def resolves(host: str) -> bool:
    """The name resolves to an address."""
    try:
        await asyncio.get_running_loop().getaddrinfo(host, 443, type=socket.SOCK_STREAM)
    except OSError:
        return False
    return True


async def diagnose(
    settings: WebSettings,
    *,
    run: Run = run_command,
    resolve: Resolve = resolves,
    static: Path = STATIC,
) -> list[Finding]:
    """Every check, in the order they are shown."""
    found = [check_data_dir(settings), check_web_ui(static), check_event_loop()]
    found += [check_isolation(settings), check_address(settings), *check_providers(settings)]
    if settings.sandbox.isolation == "docker":
        found += await check_docker(settings, run)
    found.append(await check_previews(settings, resolve))
    return found


def check_data_dir(settings: WebSettings) -> Finding:
    """The data folder exists (or can be made) and takes files."""
    folder = settings.data_dir
    try:
        folder.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=folder):
            pass
    except OSError as err:
        return Finding(
            "error",
            f"Data folder {folder} is not writable",
            str(err),
            "create it and give the service user write access",
        )
    return Finding("ok", f"Data folder {folder}")


def check_web_ui(static: Path) -> Finding:
    """The built web UI is there."""
    if (static / "index.html").is_file():
        return Finding("ok", "Web UI is built")
    return Finding(
        "error",
        "The web UI is not built",
        f"{static} has no index.html",
        "cd forge-web/frontend && npm ci && npm run build (release wheels have it)",
    )


def check_event_loop() -> Finding:
    """On Windows, the Proactor loop (subprocesses need it)."""
    if sys.platform != "win32":
        return Finding("ok", "Event loop")
    policy = type(asyncio.get_event_loop_policy()).__name__
    if "Proactor" in policy:
        return Finding("ok", "Event loop (Proactor)")
    return Finding(
        "error",
        f"Event loop {policy} cannot start subprocesses",
        fix="do not set a Selector event loop policy",
    )


def check_isolation(settings: WebSettings) -> Finding:
    """Local isolation is for one person on this machine only."""
    if settings.sandbox.isolation == "docker":
        return Finding("ok", "Isolation: one Docker container per project")
    if settings.server.host in LOOPBACK:
        return Finding(
            "warn",
            "Isolation: local (programs run as this user on this machine)",
            fix='use sandbox.isolation = "docker" before others sign in',
        )
    return Finding(
        "error",
        "Isolation: local, but the server is reachable from other machines",
        "everyone who signs in could run commands on this machine",
        'use sandbox.isolation = "docker", or listen on 127.0.0.1 only',
    )


def check_address(settings: WebSettings) -> Finding:
    """People reach a server on the network through HTTPS."""
    url = settings.server.public_url
    if settings.server.host in LOOPBACK and not url:
        return Finding("ok", f"Address {settings.base_url()} (this machine only)")
    if url.startswith("https://"):
        return Finding("ok", f"Address {url} (HTTPS)")
    return Finding(
        "warn",
        "No HTTPS address for a server on the network",
        f"server.public_url is {url or 'not set'}",
        "put a reverse proxy with HTTPS in front (deploy/Caddyfile) and set "
        'server.public_url = "https://…"',
    )


def check_providers(settings: WebSettings) -> list[Finding]:
    """Every configured sign-in provider has what it needs."""
    loaded = load_providers(settings.auth.providers)
    found = []
    for name, provider in settings.auth.providers.items():
        if name in loaded:
            found.append(Finding("ok", f"Sign-in with {name}"))
            continue
        variable = (
            provider.client_secret_env or f"FORGE_WEB_{name.upper().replace('-', '_')}_SECRET"
        )
        found.append(
            Finding(
                "warn",
                f"Sign-in with {name} is off",
                "no client secret (or, for OpenID Connect, no issuer)",
                f"set the client secret in {variable}",
            )
        )
    return found


async def check_image_forge(cli: str, image: str, run: Run) -> Finding:
    """Whether the sandbox image holds the Forge this server runs (else chats use another)."""
    code, out = await run([cli, "run", "--rm", "--pull", "never", "--network", "none",
                           "--entrypoint", "/opt/forge/bin/python", image,
                           "-I", "-m", "forge_sandbox", "fingerprint"])  # fmt: skip
    lines = out.strip().splitlines()
    theirs, ours = (lines[-1].strip() if code == 0 and lines else ""), forge_fingerprint()
    if theirs == ours:
        return Finding("ok", f"The sandbox's Forge is this server's ({ours})")
    fix = "rebuild the image: forge-web sandbox build, or forge-web/deploy/update.sh"
    if not theirs:
        return Finding("warn", "The sandbox's Forge is unknown (an image from before W18)", "", fix)
    detail = f"image {theirs}, server {ours}; projects get the new one at their next start"
    return Finding("warn", "The sandbox's Forge differs from this server's", detail, fix)


async def check_docker(settings: WebSettings, run: Run) -> list[Finding]:
    """The container CLI, its daemon, the sandbox image and gVisor."""
    cli = settings.sandbox.docker
    if shutil.which(cli) is None:
        return [
            Finding(
                "error",
                f"Docker CLI {cli!r} not found",
                fix="install Docker Engine (Linux) or Docker Desktop, or set sandbox.docker",
            )
        ]
    code, out = await run([cli, "version", "--format", "{{.Server.Version}}"])
    if code != 0:
        last = out.splitlines()[-1] if out else ""
        return [
            Finding(
                "error",
                "The Docker daemon is not reachable",
                last,
                "start Docker; the service user needs access to it (docker group)",
            )
        ]
    found = [Finding("ok", f"Docker daemon {out.splitlines()[0] if out else ''}".strip())]
    image = settings.sandbox.image
    code, _ = await run([cli, "image", "inspect", image])
    found.append(
        Finding("ok", f"Sandbox image {image}")
        if code == 0
        else Finding(
            "error",
            f"The sandbox image {image} is missing",
            "projects cannot start",
            f"forge-web sandbox build (or: {cli} pull {image})",
        )
    )
    if code == 0:
        found.append(await check_image_forge(cli, image, run))
    code, runtimes = await run([cli, "info", "--format", "{{json .Runtimes}}"])
    has_runsc = code == 0 and "runsc" in runtimes
    if has_runsc:
        found.append(Finding("ok", "gVisor (runsc) isolates the containers"))
    elif settings.sandbox.runtime == "runsc":
        found.append(
            Finding(
                "error",
                "gVisor (runsc) is required but not installed",
                fix="install gVisor and register runsc with Docker",
            )
        )
    else:
        found.append(
            Finding(
                "warn",
                "gVisor (runsc) is not installed: containers use runc",
                fix="install gVisor for stronger isolation (gvisor.dev)",
            )
        )
    return found


async def check_previews(settings: WebSettings, resolve: Resolve) -> Finding:
    """Previews have hosts browsers can reach."""
    base = preview_base(settings)
    if base is None:
        return Finding(
            "warn",
            "Live previews are off",
            "no preview.domain on a reachable server",
            "set preview.domain to a domain with wildcard DNS to this server",
        )
    if base.domain == "localhost":
        return Finding("ok", "Live previews on *.localhost (this machine only)")
    if await resolve(f"p1-doctor.{base.domain}"):
        return Finding("ok", f"Live previews on *.{base.domain}")
    return Finding(
        "warn",
        "Preview hosts do not resolve",
        f"*.{base.domain} has no DNS entry yet",
        f"add a wildcard DNS record *.{base.domain} pointing at this server",
    )


def report(findings: list[Finding]) -> str:
    """The findings as text, with fixes."""
    lines = []
    for finding in findings:
        lines.append(f"{MARKS[finding.level]}  {finding.title}")
        if finding.detail and finding.level != "ok":
            lines.append(f"      {finding.detail}")
        if finding.fix and finding.level != "ok":
            lines.append(f"      -> {finding.fix}")
    return "\n".join(lines)


def doctor(settings: WebSettings) -> int:
    """Print the findings; 1 if anything stops the server from working."""
    findings = asyncio.run(diagnose(settings))
    print(report(findings))
    return 1 if any(f.level == "error" for f in findings) else 0
