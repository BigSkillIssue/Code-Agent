"""The product from `forge app new`, installed and tested for real: its server's own tests on
PostgreSQL, the web client's tests and build, and the templates inside Forge's wheel.

Marked `fullstack` and skipped by default: it needs uv, Node 22 with npm, network access to PyPI
and npm, and a throwaway PostgreSQL database in FORGE_TEST_DATABASE_URL (its schema is dropped).
CI's `fullstack` job runs it with a PostgreSQL service: `uv run pytest -m fullstack -q`.
"""

import os
import shutil
import subprocess
import zipfile
from pathlib import Path

import pytest

from forge.app_template import write_app

pytestmark = pytest.mark.fullstack
ROOT = Path(__file__).resolve().parents[2]


def run(command: list[str], cwd: Path, **env: str) -> subprocess.CompletedProcess[str]:
    """Run a command and fail the test with its output when it fails."""
    environment = {key: value for key, value in os.environ.items() if key != "VIRTUAL_ENV"}
    result = subprocess.run(
        command, cwd=cwd, env=environment | env, capture_output=True, text=True, timeout=900
    )
    assert result.returncode == 0, f"{' '.join(command)}\n{result.stdout}\n{result.stderr}"
    return result


@pytest.fixture(scope="module")
def product(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A fresh product with its server's packages installed from the lockfile."""
    if shutil.which("uv") is None:
        pytest.skip("uv is not installed")
    folder = tmp_path_factory.mktemp("fullstack") / "tally"
    write_app(folder, "tally")
    run(["uv", "sync", "--locked"], folder / "server")
    return folder


def test_the_servers_own_tests_pass_on_postgres(product: Path) -> None:
    url = os.environ.get("FORGE_TEST_DATABASE_URL", "")
    if not url:
        pytest.skip("FORGE_TEST_DATABASE_URL names no throwaway PostgreSQL database")
    result = run(["uv", "run", "pytest", "-q"], product / "server", TEST_DATABASE_URL=url)
    assert " passed" in result.stdout


def test_the_templates_ship_in_the_wheel(tmp_path: Path) -> None:
    run(["uv", "build", "--wheel", "--out-dir", str(tmp_path)], ROOT)
    wheel = next(tmp_path.glob("forge-*.whl"))
    names = zipfile.ZipFile(wheel).namelist()
    assert "forge/templates/fullstack/server/app/main.py.tmpl" in names
    assert "forge/templates/fullstack/dot-github/workflows/ci.yml.tmpl" in names


def test_the_web_clients_tests_and_build_pass(product: Path) -> None:
    npm = shutil.which("npm")
    if npm is None:
        pytest.skip("npm is not installed")
    web = product / "web"
    run([npm, "ci", "--no-audit", "--no-fund"], web)
    run([npm, "test"], web)
    run([npm, "run", "build"], web)
    assert (web / "dist" / "index.html").is_file()


def test_the_typed_client_is_generated_from_the_snapshot(product: Path, tmp_path: Path) -> None:
    npm = shutil.which("npm")
    if npm is None:
        pytest.skip("npm is not installed")
    web = product / "web"
    if not (web / "node_modules").is_dir():
        run([npm, "ci", "--no-audit", "--no-fund"], web)
    shipped = (web / "src" / "api" / "schema.d.ts").read_text(encoding="utf-8")
    run([npm, "run", "api:types"], web)
    assert (web / "src" / "api" / "schema.d.ts").read_text(encoding="utf-8") == shipped
