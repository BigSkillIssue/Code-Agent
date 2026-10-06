"""Editing TOML config files one table at a time, keeping everything else."""

import tomllib

from forge.config_edit import header_keys, remove_table, replace_table

TEXT = """\
# my settings
[providers.local]
base_url = "http://localhost:11434/v1"

[roles]
coder = ["anthropic/claude-sonnet"]
api-tester = ["openai/gpt-5-mini"]

[models."ollama/forge-qwen3:4b"]
context_window = 8192

[mcp_servers.docs]
url = "https://docs.example.com/mcp"

[mcp_servers.docs.headers_env]
Authorization = "TOKEN"
"""


def test_header_keys_respect_quotes() -> None:
    assert header_keys('models."ollama/forge-qwen3:4b"') == ["models", "ollama/forge-qwen3:4b"]
    assert header_keys("mcp_servers . docs") == ["mcp_servers", "docs"]
    assert header_keys('a."b.c".d') == ["a", "b.c", "d"]


def test_remove_takes_the_table_and_its_sub_tables_only() -> None:
    text = remove_table(TEXT, ["mcp_servers", "docs"])
    data = tomllib.loads(text)
    assert "mcp_servers" not in data and data["roles"]["coder"] == ["anthropic/claude-sonnet"]
    assert text.startswith("# my settings\n")


def test_replace_writes_new_values_and_keeps_the_rest() -> None:
    text = replace_table(TEXT, ["models", "ollama/forge-qwen3:4b"], {"context_window": 16384})
    data = tomllib.loads(text)
    assert data["models"]["ollama/forge-qwen3:4b"] == {"context_window": 16384}
    assert data["providers"]["local"]["base_url"] == "http://localhost:11434/v1"
    assert text.startswith("# my settings\n")


def test_replace_into_an_empty_file() -> None:
    text = replace_table("", ["roles"], {"coder": ["ollama/forge-qwen3:4b"]})
    assert tomllib.loads(text) == {"roles": {"coder": ["ollama/forge-qwen3:4b"]}}
