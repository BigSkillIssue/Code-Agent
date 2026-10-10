"""The full-stack template (S64a): `forge app new` writes a complete product whose files are
filled in, parse, compile and describe themselves in a valid `forge.app.toml`."""

import json
import tomllib
from pathlib import Path

import pytest

from forge.app_manifest import AppManifest, load_manifest
from forge.app_template import (
    app_files,
    name_problem,
    render,
    template_files,
    title_of,
    title_problem,
    write_app,
)
from forge.cli import main

EXPECTED = [
    ".github/workflows/ci.yml",
    ".gitignore",
    "README.md",
    "docs/migrations.md",
    "docs/runbook.md",
    "forge.app.toml",
    "server/.python-version",
    "server/Dockerfile",
    "server/alembic.ini",
    "server/app/account.py",
    "server/app/auth.py",
    "server/app/main.py",
    "server/app/moderation.py",
    "server/app/ratelimit.py",
    "server/migrations/env.py",
    "server/migrations/script.py.mako",
    "server/migrations/versions/0001_initial_schema.py",
    "server/openapi.json",
    "server/pyproject.toml",
    "server/tests/conftest.py",
    "server/uv.lock",
]


@pytest.fixture(scope="module")
def product(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A product made by `forge app new tally-notes`."""
    folder = tmp_path_factory.mktemp("apps")
    assert main(["-C", str(folder), "app", "new", "tally-notes"]) == 0
    return folder / "tally-notes"


def files_of(root: Path) -> list[Path]:
    """Every file below a folder."""
    return sorted(path for path in root.rglob("*") if path.is_file())


def test_the_product_has_its_parts(product: Path) -> None:
    written = {path.relative_to(product).as_posix() for path in files_of(product)}
    assert set(EXPECTED) <= written


def test_no_placeholder_is_left(product: Path) -> None:
    for path in files_of(product):
        text = path.read_text(encoding="utf-8")
        assert "@@" not in text, path
    readme = (product / "README.md").read_text(encoding="utf-8")
    assert readme.startswith("# Tally Notes\n")
    assert 'name = "tally-notes"' in (product / "forge.app.toml").read_text(encoding="utf-8")


def test_every_python_file_compiles(product: Path) -> None:
    sources = [path for path in files_of(product) if path.suffix == ".py"]
    assert len(sources) >= 15
    for path in sources:
        compile(path.read_text(encoding="utf-8"), str(path), "exec")


def test_every_toml_json_and_yaml_file_parses(product: Path) -> None:
    yaml = pytest.importorskip("yaml")
    for path in files_of(product):
        text = path.read_text(encoding="utf-8")
        if path.suffix == ".toml" or path.name == "uv.lock":
            tomllib.loads(text)
        elif path.suffix == ".json":
            json.loads(text)
        elif path.suffix in (".yml", ".yaml"):
            assert isinstance(yaml.safe_load(text), dict), path


def test_the_manifest_is_valid(product: Path) -> None:
    manifest = load_manifest(product)
    assert isinstance(manifest, AppManifest), manifest
    api = manifest.services[0]
    assert (api.runtime, api.root, api.route, api.health) == (
        "python3.12",
        "server",
        "/api",
        "/healthz",
    )
    assert (product / api.root / "pyproject.toml").is_file()
    assert manifest.database is not None


def test_the_api_snapshot_names_the_product(product: Path) -> None:
    schema = json.loads((product / "server" / "openapi.json").read_text(encoding="utf-8"))
    assert schema["info"]["title"] == "Tally Notes"
    for path in ("/api/auth/signup", "/api/account/export", "/api/reports", "/api/blocks"):
        assert path in schema["paths"]


def test_the_server_pins_its_packages(product: Path) -> None:
    project = tomllib.loads((product / "server" / "pyproject.toml").read_text(encoding="utf-8"))
    for requirement in project["project"]["dependencies"]:
        assert "==" in requirement, requirement
    lock = tomllib.loads((product / "server" / "uv.lock").read_text(encoding="utf-8"))
    locked = {package["name"] for package in lock["package"]}
    assert {"fastapi", "sqlalchemy", "alembic", "psycopg", "argon2-cffi"} <= locked


def test_files_keep_unix_line_endings(product: Path) -> None:
    for path in files_of(product):
        assert b"\r\n" not in path.read_bytes(), path


def test_names_and_titles() -> None:
    assert name_problem("tally-notes") is None
    for wrong in ("Tally", "1app", "a_b", "", "x" * 41):
        assert name_problem(wrong)
    assert title_of("tally-notes") == "Tally Notes"
    assert title_problem("Tally Notes 2") is None
    for wrong in ('Say "hi"', "Café", "<b>", " leading"):
        assert title_problem(wrong)


def test_a_title_can_be_given(tmp_path: Path) -> None:
    assert main(["-C", str(tmp_path), "app", "new", "notes", "--title", "My Notes"]) == 0
    readme = (tmp_path / "notes" / "README.md").read_text(encoding="utf-8")
    assert readme.startswith("# My Notes\n")
    assert main(["-C", str(tmp_path), "app", "new", "other", "--title", "Bad <title>"]) == 1


def test_new_refuses_a_folder_in_use_and_a_bad_name(tmp_path: Path) -> None:
    (tmp_path / "taken").mkdir()
    (tmp_path / "taken" / "file.txt").write_text("mine", encoding="utf-8")
    assert main(["-C", str(tmp_path), "app", "new", "taken"]) == 1
    assert (tmp_path / "taken" / "file.txt").read_text(encoding="utf-8") == "mine"
    assert main(["-C", str(tmp_path), "app", "new", "Bad_Name"]) == 1
    with pytest.raises(FileExistsError):
        write_app(tmp_path / "taken", "taken")


def test_templates_are_package_data() -> None:
    files = template_files()
    assert "server/app/main.py" in files
    assert all(not path.startswith("dot-") and "/dot-" not in path for path in files)
    assert app_files("x")["forge.app.toml"].startswith(b"# How Forge Web hosts X.")


def test_an_unknown_placeholder_is_a_template_bug() -> None:
    assert render("@@name@@ and @@title@@", {"name": "a", "title": "A"}) == "a and A"
    with pytest.raises(KeyError):
        render("@@nope@@", {"name": "a"})
