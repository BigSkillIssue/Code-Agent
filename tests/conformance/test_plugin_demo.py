"""examples/plugin_demo adds a hook and an MCP tool with zero changes to src/forge."""

import os
import shutil
from pathlib import Path

import pytest

from forge.config import load_config, set_trusted
from forge.local.memory_store import MemoryStore
from forge.providers.base import ToolCall
from forge.tools import call_tool
from forge.wiring import close_session, open_session
from support import NoExecutor, ScriptedRenderer, init_repo

DEMO = Path(__file__).resolve().parents[2] / "examples" / "plugin_demo"


async def test_plugin_demo_hook_and_tool(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    for name in list(os.environ):
        if name.startswith("FORGE_"):
            monkeypatch.delenv(name)
    monkeypatch.setenv("FORGE_HOME", str(tmp_path / "home"))
    root = tmp_path / "plugin_demo"
    shutil.copytree(DEMO, root)
    init_repo(root)
    set_trusted(root)
    cfg = load_config(root)
    assert cfg.warnings == [] and "demo" in cfg.mcp_servers
    ctx = await open_session(
        root, cfg, ScriptedRenderer(), store=MemoryStore(), executor=NoExecutor()
    )
    try:
        count = await call_tool(
            ctx, ToolCall(id="1", name="mcp__demo__word_count", arguments={"text": "one two three"})
        )
        assert count.ok and count.text == "3 words"
        written = await call_tool(
            ctx,
            ToolCall(id="2", name="write_file", arguments={"path": "count.txt", "content": "3\n"}),
        )
        assert written.ok
        log = (root / ".forge" / "changes.log").read_text()
        assert log.strip().endswith("write_file count.txt")
    finally:
        await close_session(ctx)
