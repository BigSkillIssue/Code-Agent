"""`forge-web doctor`: what is missing for a working server, and how to fix it; the wheel
carries the built web UI."""

import shutil
import stat
import subprocess
import sys
import zipfile
from pathlib import Path
from typing import Any

import pytest

from forge_sandbox.fingerprint import forge_fingerprint
from forge_web.cli import main
from forge_web.doctor import Finding, diagnose
from forge_web.settings import load_settings

POSIX_ONLY = pytest.mark.skipif(sys.platform == "win32", reason="the fake docker is a shell script")
SERVER = Path(__file__).parents[1] / "packages" / "server"


def settings_with(tmp_path: Path, **overrides: Any) -> Any:
    values = {"data_dir": str(tmp_path / "data"), **overrides}
    return load_settings(tmp_path / "forge-web.toml", environ={}, overrides=values)


def fake_docker(
    tmp_path: Path, *, image: bool, runtimes: str = '{"runc":{}}', forge: str | None = None
) -> str:
    """A `docker` that answers like a running daemon (with or without the sandbox image); in
    the image `forge-sandbox fingerprint` prints `forge` (the server's Forge if not given)."""
    printed = forge_fingerprint() if forge is None else forge
    script = tmp_path / "fake-docker"
    script.write_text(
        "#!/bin/sh\n"
        'case "$1" in\n'
        "  version) echo 27.1.1 ;;\n"
        f"  image) exit {0 if image else 1} ;;\n"
        f"  info) echo '{runtimes}' ;;\n"
        f"  run) {f'echo {printed}' if printed else 'exit 2'} ;;\n"
        "esac\n"
    )
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return str(script)


def by_title(findings: list[Finding], words: str) -> Finding:
    # The data folder's path holds the test's name, so that finding is left out.
    checks = [f for f in findings if not f.title.startswith("Data folder")]
    found = [f for f in checks if words.lower() in f.title.lower()]
    assert found, f"no finding about {words!r}: {[f.title for f in findings]}"
    return found[0]


async def built_ui(tmp_path: Path) -> Path:
    static = tmp_path / "static"
    static.mkdir(parents=True)
    (static / "index.html").write_text("<!doctype html>")
    return static


async def test_a_missing_docker_is_an_error(tmp_path: Path) -> None:
    settings = settings_with(tmp_path, **{"sandbox.docker": "no-such-docker-anywhere"})
    findings = await diagnose(settings, static=await built_ui(tmp_path))
    docker = by_title(findings, "docker cli")
    assert docker.level == "error" and "install" in docker.fix.lower()


@POSIX_ONLY
async def test_a_missing_sandbox_image_names_the_fix(tmp_path: Path) -> None:
    docker = fake_docker(tmp_path, image=False)
    settings = settings_with(tmp_path, **{"sandbox.docker": docker, "sandbox.image": "img:1"})
    findings = await diagnose(settings, static=await built_ui(tmp_path))
    assert by_title(findings, "docker daemon").level == "ok"
    image = by_title(findings, "sandbox image")
    assert image.level == "error" and "forge-web sandbox build" in image.fix
    ready = settings_with(tmp_path, **{"sandbox.docker": fake_docker(tmp_path, image=True)})
    checked = await diagnose(ready, static=await built_ui(tmp_path / "x"))
    assert by_title(checked, "sandbox image").level == "ok"
    assert by_title(checked, "gvisor").level == "warn"  # runc only: works, less isolated
    strict = settings_with(tmp_path, **{"sandbox.docker": fake_docker(tmp_path, image=True),
                                        "sandbox.runtime": "runsc"})  # fmt: skip
    assert by_title(await diagnose(strict, static=await built_ui(tmp_path / "y")),
                    "gvisor").level == "error"  # fmt: skip


@POSIX_ONLY
async def test_a_sandbox_image_with_another_forge_is_a_warning(tmp_path: Path) -> None:
    cases = {"same": (None, "ok"), "older": ("0123456789ab", "warn"), "unknown": ("", "warn")}
    for name, (forge, level) in cases.items():
        docker = fake_docker(tmp_path, image=True, forge=forge)
        settings = settings_with(tmp_path, **{"sandbox.docker": docker})
        found = by_title(await diagnose(settings, static=await built_ui(tmp_path / name)),
                         "sandbox's forge")  # fmt: skip
        assert found.level == level, (name, found)
        if level == "warn":
            assert "forge-web sandbox build" in found.fix and "update.sh" in found.fix


async def test_local_isolation_and_addresses(tmp_path: Path) -> None:
    static = await built_ui(tmp_path)
    local = settings_with(tmp_path, **{"sandbox.isolation": "local"})
    assert by_title(await diagnose(local, static=static), "isolation").level == "warn"
    exposed = settings_with(tmp_path, **{"sandbox.isolation": "local", "server.host": "0.0.0.0"})
    findings = await diagnose(exposed, static=static)
    assert by_title(findings, "isolation").level == "error"
    assert by_title(findings, "https").level == "warn"  # reachable, but no https public_url


async def test_previews_sign_in_providers_and_the_web_ui(tmp_path: Path) -> None:
    async def no_dns(host: str) -> bool:
        return False

    async def dns(host: str) -> bool:
        return host.endswith(".preview.example.net")

    base = {"sandbox.isolation": "local", "preview.domain": "preview.example.net",
            "auth.providers": {"google": {"client_id": "x"}}}  # fmt: skip
    settings = settings_with(tmp_path, **base)
    findings = await diagnose(settings, static=tmp_path / "nothing", resolve=no_dns)
    assert by_title(findings, "preview").level == "warn"
    assert "*.preview.example.net" in by_title(findings, "preview").detail
    assert by_title(findings, "google").level == "warn"  # no client secret
    assert by_title(findings, "web ui").level == "error"
    fine = await diagnose(settings, static=await built_ui(tmp_path), resolve=dns)
    assert by_title(fine, "preview").level == "ok" and by_title(fine, "web ui").level == "ok"


def test_the_command_prints_fixes_and_fails_on_errors(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    config = tmp_path / "forge-web.toml"
    config.write_text('[sandbox]\ndocker = "no-such-docker-anywhere"\n')
    code = main(["--config", str(config), "--data-dir", str(tmp_path / "data"), "doctor"])
    out = capsys.readouterr().out
    assert code == 1
    assert "FAIL" in out and "docker" in out.lower() and "->" in out


@pytest.mark.skipif(shutil.which("uv") is None, reason="needs uv to build the wheel")
def test_the_wheel_carries_the_web_ui(tmp_path: Path) -> None:
    static = SERVER / "src" / "forge_web" / "static"
    index = static / "index.html"
    placeholder = not index.exists()  # the gate does not build the UI; a stand-in will do
    if placeholder:
        static.mkdir(exist_ok=True)
        index.write_text("<!doctype html><title>Forge</title>")
    try:
        subprocess.run(["uv", "build", "--wheel", "--out-dir", str(tmp_path), str(SERVER)],
                       check=True, capture_output=True, timeout=300)  # fmt: skip
    finally:
        if placeholder:
            index.unlink()
    wheel = next(tmp_path.glob("forge_web-*.whl"))
    names = zipfile.ZipFile(wheel).namelist()
    assert "forge_web/static/index.html" in names
    assert "forge_web/db/migrations/versions/0005_settings.py" in names
