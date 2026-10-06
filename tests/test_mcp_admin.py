"""Adding, listing and removing MCP servers: `forge mcp ...` and `/mcp` (S56)."""

import sys
import tomllib
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from forge.cli import main
from forge.commands import handle_command
from forge.config import ForgeConfig, McpServerConfig, set_trusted
from forge.ctx import Ctx
from forge.mcp_admin import add_server, from_json, list_servers, remove_server
from forge.mcp_cli import split_words
from forge.mcp_client import McpHub
from forge.wiring import close_session
from support import make_ctx

STUB = str(Path(__file__).parent / "fixtures" / "mcp_stub.py")
USER_TOML = """\
# my settings
[providers.local]
base_url = "http://localhost:11434/v1"

[mcp_servers.old]
command = ["old-server"]
"""


@pytest.fixture(autouse=True)
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    folder = tmp_path / "home"
    folder.mkdir()
    monkeypatch.setenv("FORGE_HOME", str(folder))
    return folder


def read(path: Path) -> dict[str, object]:
    return tomllib.loads(path.read_text(encoding="utf-8"))


def test_add_keeps_the_rest_of_the_file(home: Path, tmp_project: Path) -> None:
    (home / "forge.toml").write_text(USER_TOML)
    server = McpServerConfig(command=["npx", "-y", "server-github"], env_keys=["GITHUB_TOKEN"])
    path, warnings = add_server(tmp_project, "user", "github", server)
    assert path == home / "forge.toml" and warnings == []
    text = path.read_text()
    assert text.startswith("# my settings\n[providers.local]")
    data = read(path)
    assert data["mcp_servers"] == {  # type: ignore[comparison-overlap]
        "old": {"command": ["old-server"]},
        "github": {"command": ["npx", "-y", "server-github"], "env_keys": ["GITHUB_TOKEN"]},
    }


def test_adding_again_replaces_and_remove_takes_only_that_server(
    home: Path, tmp_project: Path
) -> None:
    (home / "forge.toml").write_text(USER_TOML)
    add_server(tmp_project, "user", "docs", McpServerConfig(url="https://a.example.com/mcp"))
    docs = McpServerConfig(url="https://b.example.com/mcp", headers_env={"Authorization": "T"})
    add_server(tmp_project, "user", "docs", docs)
    data = read(home / "forge.toml")
    assert data["mcp_servers"]["docs"] == {  # type: ignore[index]
        "url": "https://b.example.com/mcp",
        "headers_env": {"Authorization": "T"},
    }
    remove_server(tmp_project, "docs")
    data = read(home / "forge.toml")
    assert data["mcp_servers"] == {"old": {"command": ["old-server"]}}  # type: ignore[comparison-overlap]
    assert data["providers"] == {"local": {"base_url": "http://localhost:11434/v1"}}
    with pytest.raises(ValueError, match="no MCP server 'docs'"):
        remove_server(tmp_project, "docs")


def test_project_scope_warns_until_trusted(tmp_project: Path) -> None:
    server = McpServerConfig(command=["srv"])
    path, warnings = add_server(tmp_project, "project", "srv", server)
    assert path == tmp_project / ".forge" / "config.toml"
    assert warnings and "forge trust" in warnings[0]
    set_trusted(tmp_project)
    _, warnings = add_server(tmp_project, "project", "srv", server)
    assert warnings == []
    assert [(s.name, s.scope) for s in list_servers(tmp_project)] == [("srv", "project")]


def test_invalid_entries_are_refused(tmp_project: Path) -> None:
    with pytest.raises(ValueError, match="name"):
        add_server(tmp_project, "user", "bad name!", McpServerConfig(command=["x"]))
    with pytest.raises(ValueError, match="command or a url"):
        add_server(tmp_project, "user", "both", McpServerConfig(command=["x"], url="https://x"))


def test_claude_style_json_is_converted() -> None:
    stdio, notes = from_json(
        '{"command": "npx", "args": ["-y", "pkg"], "env": {"GITHUB_TOKEN": "ghp_secret"}}'
    )
    assert stdio == McpServerConfig(command=["npx", "-y", "pkg"], env_keys=["GITHUB_TOKEN"])
    assert notes and "GITHUB_TOKEN" in notes[0] and "ghp_secret" not in notes[0]
    http, notes = from_json(
        '{"type": "http", "url": "https://docs.example.com/mcp",'
        ' "headers": {"Authorization": "Bearer ${DOCS_TOKEN}"}}'
    )
    assert http == McpServerConfig(
        url="https://docs.example.com/mcp", headers_env={"Authorization": "DOCS_TOKEN"}
    )
    assert notes and "Bearer" in notes[0]


def test_cli_add_list_get_remove(
    home: Path, tmp_project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = ["-C", str(tmp_project)]
    assert main([*root, "mcp", "add", "stub", "--", sys.executable, STUB]) == 0
    assert "added MCP server stub" in capsys.readouterr().out
    assert main([*root, "mcp", "list"]) == 0
    listing = capsys.readouterr().out
    assert "stub" in listing and "user" in listing and "connected" in listing and "tools" in listing
    assert main([*root, "mcp", "get", "stub"]) == 0
    assert STUB in capsys.readouterr().out
    assert main([*root, "mcp", "add-json", "gh", '{"command": "gh-mcp"}']) == 0
    capsys.readouterr()
    assert main([*root, "mcp", "remove", "stub"]) == 0
    assert set(read(home / "forge.toml")["mcp_servers"]) == {"gh"}  # type: ignore[arg-type]
    assert main([*root, "mcp", "remove", "stub"]) == 1


@pytest.fixture
async def ctx_mcp(tmp_project: Path) -> AsyncIterator[Ctx]:
    ctx = make_ctx(tmp_project, cfg=ForgeConfig())
    yield ctx
    await close_session(ctx)


async def test_slash_mcp_adds_reconnects_and_removes_live(ctx_mcp: Ctx, home: Path) -> None:
    added = (await handle_command(ctx_mcp, f"/mcp add stub -- {sys.executable} {STUB}")).text
    assert "stub: connected" in added
    hub = ctx_mcp.state.mcp
    assert isinstance(hub, McpHub) and hub.tool("mcp__stub__add") is not None
    status = (await handle_command(ctx_mcp, "/mcp")).text
    assert "stub" in status and "connected" in status
    again = (await handle_command(ctx_mcp, "/mcp reconnect stub")).text
    assert "stub: connected" in again
    removed = (await handle_command(ctx_mcp, "/mcp remove stub")).text
    assert "removed" in removed and hub.tool("mcp__stub__add") is None
    assert "stub" not in read(home / "forge.toml").get("mcp_servers", {})  # type: ignore[operator]


def test_slash_arguments_keep_windows_paths(monkeypatch: pytest.MonkeyPatch) -> None:
    line = 'add s -- C:\\Python\\python.exe "C:\\My Tools\\stub.py"'
    monkeypatch.setattr("forge.mcp_cli.os.name", "nt")
    assert split_words(line) == [
        "add",
        "s",
        "--",
        "C:\\Python\\python.exe",
        "C:\\My Tools\\stub.py",
    ]
    monkeypatch.setattr("forge.mcp_cli.os.name", "posix")
    assert split_words('add s -- python "my stub.py"') == ["add", "s", "--", "python", "my stub.py"]
