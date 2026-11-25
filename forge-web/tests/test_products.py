"""Forge Web's product factory (W51): the templates render, and placing a product never
overwrites what someone changed."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest
from forge.app_manifest import load_manifest, parse_manifest

from forge_sandbox.products.place import add, create
from forge_sandbox.products.render import ProductError, product_files

ALL = ["payments", "apple", "android", "windows", "digital_goods"]
DOMAIN = "apps.example.net"


@pytest.mark.parametrize(("kind", "features"), [
    ("fullstack", []), ("fullstack", ["payments"]), ("fullstack", ALL), ("site", []),
])  # fmt: skip
def test_every_variant_renders_cleanly(kind: str, features: list[str]) -> None:
    files = product_files(kind, "tally-notes", "Tally Notes", features, DOMAIN)
    for path, data in files.items():
        if path.endswith((".jar", ".ico", ".png")):
            continue
        text = data.decode()
        assert "@@" not in text, path
        assert "\r\n" not in text or path.endswith(".bat"), path
        if path.endswith(".json"):
            json.loads(text)
    manifest = parse_manifest(files["forge.app.toml"].decode())
    assert not isinstance(manifest, list), manifest
    assert manifest.name == "tally-notes"
    assert b"<title>Tally Notes</title>" in files["web/index.html"]
    package = json.loads(files["web/package.json"])
    lock = json.loads(files["web/package-lock.json"])["packages"][""]
    assert lock["dependencies"] == package["dependencies"]
    assert lock["devDependencies"] == package["devDependencies"]


def test_a_site_has_no_server_and_takes_no_server_features() -> None:
    files = product_files("site", "flower-shop", None, (), DOMAIN)
    assert not any(path.startswith("server/") for path in files)
    manifest = parse_manifest(files["forge.app.toml"].decode())
    assert not isinstance(manifest, list) and manifest.database is None
    assert [s.runtime for s in manifest.services] == ["static"]
    with pytest.raises(ProductError, match="no server"):
        product_files("site", "flower-shop", None, ["android"], DOMAIN)
    for bad in ("Flower Shop", "-x"):
        with pytest.raises(ProductError):
            product_files("site", bad, None, (), DOMAIN)


def test_apps_point_at_the_product_on_forge_servers() -> None:
    files = product_files("fullstack", "tally", None, ["android", "windows"], DOMAIN)
    assert b"https://tally.staging.apps.example.net" in files["android/app/build.gradle.kts"]
    assert b"https://tally.staging.apps.example.net" in files["windows/Core/Server.cs"]
    assert files["android/gradle/wrapper/gradle-wrapper.jar"].startswith(b"PK")


def test_creating_a_product_keeps_what_is_already_there(tmp_path: Path) -> None:
    (tmp_path / "README.md").write_text("# my notes\n")
    placed = create(tmp_path, "fullstack", "tally", None, (), DOMAIN)
    assert "README.md" in placed.to_merge and "# my notes" in placed.to_merge["README.md"]
    assert (tmp_path / "README.md").read_text() == "# my notes\n"
    assert "server/app/main.py" in placed.written
    assert not isinstance(load_manifest(tmp_path / "forge.app.toml"), list)
    with pytest.raises(ProductError, match="already is a product"):
        create(tmp_path, "site", "tally", None, (), DOMAIN)


def test_adding_clients_later(tmp_path: Path) -> None:
    create(tmp_path, "fullstack", "tally", None, (), DOMAIN)
    layout = tmp_path / "web/src/components/Layout.tsx"
    layout.write_text(layout.read_text().replace("Sign out", "Log out"))  # the agent's change
    placed = add(tmp_path, None, ["android", "windows"], DOMAIN)
    assert any(path.startswith("android/") for path in placed.written)
    assert "forge.app.toml" in placed.written  # unchanged by anyone: replaced
    manifest = load_manifest(tmp_path / "forge.app.toml")
    assert not isinstance(manifest, list) and manifest.clients == ["web", "android", "windows"]
    assert "web/src/components/Layout.tsx" in placed.to_merge  # changed: handed back as a diff
    assert "+" in placed.to_merge["web/src/components/Layout.tsx"]
    assert "Log out" in layout.read_text()
    again = add(tmp_path, None, ["android"], DOMAIN)
    assert again.written == []
    with pytest.raises(ProductError, match="no such feature"):
        add(tmp_path, None, ["fax"], DOMAIN)


@pytest.mark.npm
@pytest.mark.parametrize("kind", ["site", "fullstack"])
def test_the_web_client_installs_tests_and_builds(tmp_path: Path, kind: str) -> None:
    if shutil.which("npm") is None:
        pytest.skip("npm is not installed")
    create(tmp_path, kind, "tally", None, (), DOMAIN)
    web = tmp_path / "web"
    for command in (
        ["npm", "ci", "--no-audit", "--no-fund"],
        ["npm", "test"],
        ["npm", "run", "build"],
    ):
        done = subprocess.run(command, cwd=web, capture_output=True, text=True, timeout=600)
        assert done.returncode == 0, (command, done.stdout[-2000:], done.stderr[-2000:])
    assert (web / "dist" / "index.html").is_file()
