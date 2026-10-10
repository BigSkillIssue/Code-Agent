"""The app manifest (S63): `forge.app.toml` describes a product for hosting; every problem in it
is reported as a value, and secrets are named, never given."""

from pathlib import Path

from forge.app_manifest import (
    MANIFEST,
    AppManifest,
    ManifestProblem,
    load_manifest,
    parse_manifest,
)

FULL = """\
version = 1
name = "tally"
resource_class = "medium"
clients = ["web", "apple"]
secrets = ["SMTP_RELAY_TOKEN"]

[env]
LOG_LEVEL = "info"

[[services]]
name = "api"
runtime = "python3.12"
root = "server"
command = ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
port = 8000
health = "/healthz"
route = "/api"

[[services]]
name = "web"
runtime = "static"
root = "web"
build = ["npm", "run", "build"]
port = 8080
route = "/"

[database]
engine = "postgres16"

[storage]
max_gb = 5

[mail]
daily_limit = 500

[payments]
kind = "relay"
"""

MINIMAL = """\
name = "notes"

[[services]]
name = "api"
runtime = "node22"
command = ["node", "server.js"]
port = 3000
"""


def problems(text: str) -> list[ManifestProblem]:
    """The problems of a manifest that must not load."""
    result = parse_manifest(text)
    assert isinstance(result, list), "the manifest loaded but should not"
    return result


def fields(text: str) -> list[str]:
    """The fields the problems of a manifest name."""
    return [problem.field for problem in problems(text)]


def test_a_full_manifest_loads() -> None:
    manifest = parse_manifest(FULL)
    assert isinstance(manifest, AppManifest)
    assert [service.name for service in manifest.services] == ["api", "web"]
    assert manifest.services[0].command[0] == "uvicorn"
    assert manifest.services[1].route == "/"
    assert manifest.database is not None and manifest.database.engine == "postgres16"
    assert manifest.storage is not None and manifest.storage.max_gb == 5
    assert manifest.mail is not None and manifest.mail.daily_limit == 500
    assert manifest.payments.kind == "relay"
    assert manifest.secrets == ["SMTP_RELAY_TOKEN"]
    assert manifest.clients == ["web", "apple"]


def test_a_minimal_manifest_gets_the_defaults() -> None:
    manifest = parse_manifest(MINIMAL)
    assert isinstance(manifest, AppManifest)
    assert manifest.resource_class == "small"
    assert manifest.services[0].health == "/healthz"
    assert manifest.services[0].root == "."
    assert manifest.database is None and manifest.mail is None and manifest.storage is None
    assert manifest.clients == ["web"]
    assert manifest.payments.kind == "none"


def test_missing_fields_and_wrong_types_are_named() -> None:
    assert fields('[[services]]\nname = "api"\nruntime = "node22"\ncommand = ["x"]\n') == [
        "name",
        "services.0.port",
    ]
    assert fields(MINIMAL.replace("port = 3000", 'port = "three"')) == ["services.0.port"]
    assert fields('name = "notes"\n') == ["services"]
    assert fields("colour = 1\n" + MINIMAL) == ["colour"]


def test_a_secret_with_a_value_is_refused() -> None:
    found = problems(MINIMAL + '\n[secrets]\nSTRIPE_KEY = "sk_live_123"\n')
    assert [problem.field for problem in found] == ["secrets.STRIPE_KEY"]
    assert "names only" in found[0].message
    assert "sk_live_123" not in found[0].message
    found = problems(MINIMAL + '\n[env]\nAPI_TOKEN = "abc"\nDB_PASSWORD = "x"\n')
    assert [problem.field for problem in found] == ["env.API_TOKEN", "env.DB_PASSWORD"]
    assert "secrets" in found[0].message and "abc" not in found[0].message


def test_an_unknown_runtime_is_refused() -> None:
    found = problems(MINIMAL.replace("node22", "ruby3"))
    assert [problem.field for problem in found] == ["services.0.runtime"]
    assert "python3.12" in found[0].message


def test_two_services_on_one_port_are_refused() -> None:
    text = FULL.replace("port = 8080", "port = 8000")
    found = problems(text)
    assert [problem.field for problem in found] == ["services.1.port"]
    assert "'api'" in found[0].message


def test_service_rules() -> None:
    assert fields(MINIMAL + 'health = "healthz"\n') == ["services.0.health"]
    assert fields(MINIMAL.replace('command = ["node", "server.js"]\n', "")) == [
        "services.0.command"
    ]
    assert fields(MINIMAL.replace('name = "api"', 'name = "API"')) == ["services.0.name"]
    assert fields(MINIMAL.replace("port = 3000", "port = 80")) == ["services.0.port"]
    two = FULL.replace('name = "web"', 'name = "api"').replace('route = "/"', 'route = "/api"')
    assert fields(two) == ["services.1.name", "services.1.route"]
    assert fields(FULL.replace('route = "/"', 'route = "web"')) == ["services.1.route"]


def test_env_and_secret_names() -> None:
    lower = MINIMAL.replace('name = "notes"', 'name = "notes"\nsecrets = ["lower"]')
    assert fields(lower) == ["secrets.0"]
    text = MINIMAL.replace('name = "notes"', 'name = "notes"\nsecrets = ["LOG_LEVEL", "PORT"]')
    found = fields(text + '\n[env]\nLOG_LEVEL = "info"\nDATABASE_URL = "x"\n')
    assert found == ["env.DATABASE_URL", "secrets.0", "secrets.1"]


def test_a_toml_syntax_error_names_its_line() -> None:
    found = problems('name = "notes"\n\n[[services]\n')
    assert len(found) == 1
    assert found[0].line == 3
    assert found[0].field == ""


def test_every_problem_is_reported() -> None:
    text = MINIMAL.replace("node22", "ruby3").replace('name = "notes"', 'name = "No Tes"')
    assert fields(text + '\n[env]\nAPI_TOKEN = "abc"\n') == [
        "env.API_TOKEN",
        "name",
        "services.0.runtime",
    ]


def test_load_reads_a_file_or_a_folder(tmp_path: Path) -> None:
    missing = load_manifest(tmp_path)
    assert isinstance(missing, list) and MANIFEST in missing[0].message
    (tmp_path / MANIFEST).write_text(MINIMAL, encoding="utf-8")
    assert isinstance(load_manifest(tmp_path), AppManifest)
    assert isinstance(load_manifest(tmp_path / MANIFEST), AppManifest)
