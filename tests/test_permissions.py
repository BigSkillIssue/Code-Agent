"""Permission rules and defaults (docs/PLAN.md: Safety, permissions)."""

from pathlib import Path
from typing import Any

import pytest

from forge.config import ApprovalConfig, ForgeConfig, PermissionsConfig, SandboxConfig
from forge.ctx import Ctx
from forge.ports import Approval
from forge.providers.base import ToolCall, ToolResult
from forge.runtime.permissions import Permissions, is_read_only_command
from forge.runtime.rules import RuleError, parse_rule, rule_matches
from forge.tools import REGISTRY, call_tool
from support import ScriptedRenderer, make_ctx

ROOT = Path("/work/project")


@pytest.mark.parametrize(
    ("rule", "tool", "spec", "expected"),
    [
        ("bash(git status*)", "bash", "git status", True),
        ("bash(git status*)", "bash", "git status --short", True),
        ("bash(git status*)", "bash", "git push", False),
        ("bash(git push *)", "bash", "git  push   origin main", True),
        ("bash(npm run test:*)", "bash", "npm run test", True),
        ("bash(npm run test:*)", "bash", "npm run test -- --watch", True),
        ("bash(npm run test:*)", "bash", "npm run testing", False),
        ("bash(rm -rf *)", "bash", "rm -rf /tmp/x", True),
        ("bash(rm -rf *)", "powershell", "rm -rf /tmp/x", False),
        ("bash", "bash", "anything at all", True),
        ("Bash(ls)", "bash", "ls", True),
        ("read_file(./.env*)", "read_file", ".env", True),
        ("read_file(./.env*)", "read_file", "/work/project/.env.local", True),
        ("read_file(./.env*)", "read_file", "src/.env", False),
        ("read_file(./.env*)", "edit_file", ".env", True),
        ("read_file(./.env*)", "write_file", ".env.prod", True),
        ("read_file(./.env*)", "grep", ".env", False),
        ("edit_file(src/**)", "edit_file", "src/a/b/c.py", True),
        ("edit_file(src/*.py)", "edit_file", "src/a/b.py", False),
        ("edit_file(src/*.py)", "edit_file", "src/b.py", True),
        ("edit_file(**/*.lock)", "edit_file", "deep/x/uv.lock", True),
        ("read_file(~/.ssh/**)", "read_file", str(Path("~/.ssh/id_rsa").expanduser()), True),
        ("web_fetch(domain:docs.python.org)", "web_fetch", "domain:docs.python.org", True),
        ("web_fetch(domain:docs.python.org)", "web_fetch", "domain:evil.org", False),
        ("web_fetch(domain:*.python.org)", "web_fetch", "domain:peps.python.org", True),
        ("web_fetch(domain:*.python.org)", "web_fetch", "domain:python.org", True),
        ("web_search", "web_search", "", True),
        ("spawn_agent(review*)", "spawn_agent", "reviewer", True),
    ],
)
def test_rule_matrix(rule: str, tool: str, spec: str, expected: bool) -> None:
    assert rule_matches(parse_rule(rule), tool, spec, ROOT) is expected


def test_bad_rule_is_refused() -> None:
    with pytest.raises(RuleError):
        parse_rule("bash(git status")


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        ("ls -la", True),
        ("git status", True),
        ("git diff HEAD~1 | head -20", True),
        ("rg TODO src | wc -l", True),
        ("Get-ChildItem", True),
        ("git push", False),
        ("cat a > b", False),
        ("ls; rm -rf x", False),
        ("ls && touch x", False),
        ("find . -delete", False),
        ("echo $(rm x)", False),
        ("python script.py", False),
    ],
)
def test_read_only_commands(command: str, expected: bool) -> None:
    assert is_read_only_command(command) is expected


def decide(cfg: ForgeConfig, tool: str, **args: Any) -> str:
    permissions = Permissions(cfg)
    ctx = make_ctx(Path("/work/project"), cfg=cfg)
    return permissions.check(REGISTRY[tool], args, ctx).action


def test_order_deny_then_ask_then_allow() -> None:
    rules = PermissionsConfig(
        allow=["bash(git *)"], ask=["bash(git push *)"], deny=["bash(git push --force*)"]
    )
    cfg = ForgeConfig(permissions=rules, approval=ApprovalConfig(policy="always"))
    assert decide(cfg, "bash", command="git push --force origin") == "deny"
    assert decide(cfg, "bash", command="git push origin") == "ask"
    assert decide(cfg, "bash", command="git commit -m x") == "run"
    assert decide(cfg, "bash", command="make") == "ask"  # policy 'always', no rule
    assert decide(cfg, "bash", command="git status") == "run"


def test_defaults_by_policy_and_mode() -> None:
    never = ForgeConfig(approval=ApprovalConfig(policy="never"))
    assert decide(never, "web_fetch", url="https://x.org") == "deny"
    assert decide(never, "bash", command="make") == "run"
    always = ForgeConfig(approval=ApprovalConfig(policy="always"))
    assert decide(always, "write_file", path="a", content="") == "ask"
    assert decide(always, "read_file", path="a") == "run"
    on_request = ForgeConfig()
    assert decide(on_request, "write_file", path="a", content="") == "run"
    assert decide(on_request, "web_fetch", url="https://x.org") == "ask"
    read_only = ForgeConfig(sandbox=SandboxConfig(mode="read-only"))
    assert decide(read_only, "edit_file", path="a", old="x", new="y") == "deny"
    assert decide(read_only, "read_file", path="a") == "run"


def test_web_fetch_rules_use_the_domain() -> None:
    cfg = ForgeConfig(permissions=PermissionsConfig(allow=["web_fetch(domain:docs.python.org)"]))
    assert decide(cfg, "web_fetch", url="https://docs.python.org/3/") == "run"
    assert decide(cfg, "web_fetch", url="https://evil.example/") == "ask"


def test_remembered_approval_never_beats_deny(tmp_project: Path) -> None:
    cfg = ForgeConfig(permissions=PermissionsConfig(deny=["web_fetch(domain:evil.org)"]))
    permissions = Permissions(cfg)
    ctx = make_ctx(tmp_project, cfg=cfg)
    permissions.remember(REGISTRY["web_fetch"], {"url": "https://evil.org/x"})
    permissions.remember(REGISTRY["web_fetch"], {"url": "https://good.org/x"})
    check = permissions.check
    assert check(REGISTRY["web_fetch"], {"url": "https://evil.org/y"}, ctx).action == "deny"
    assert check(REGISTRY["web_fetch"], {"url": "https://good.org/y"}, ctx).action == "run"


async def run(ctx: Ctx, name: str, **arguments: Any) -> ToolResult:
    return await call_tool(ctx, ToolCall(id="c1", name=name, arguments=arguments))


async def test_read_file_deny_also_blocks_edit_file(tmp_project: Path) -> None:
    cfg = ForgeConfig(permissions=PermissionsConfig(deny=["read_file(./.env*)"]))
    ctx = make_ctx(tmp_project, cfg=cfg)
    (tmp_project / ".env").write_text("KEY=1\n")
    assert (await run(ctx, "read_file", path=".env")).code == "permission_denied"
    edit = await run(ctx, "edit_file", path=".env", old="1", new="2")
    assert edit.code == "permission_denied"
    write = await run(ctx, "write_file", path=".env.local", content="x")
    assert write.code == "permission_denied"
    assert (tmp_project / ".env").read_text() == "KEY=1\n"


async def test_apply_patch_checks_every_path(tmp_project: Path) -> None:
    cfg = ForgeConfig(permissions=PermissionsConfig(deny=["read_file(./secrets/**)"]))
    ctx = make_ctx(tmp_project, cfg=cfg)
    patch = (
        "*** Begin Patch\n*** Add File: ok.txt\n+a\n*** Add File: secrets/x.txt\n+b\n*** End Patch"
    )
    result = await run(ctx, "apply_patch", patch=patch)
    assert result.code == "permission_denied" and "secrets/x.txt" in result.text
    assert not (tmp_project / "ok.txt").exists()


async def test_apply_patch_ask_rule_uses_the_renderer(tmp_project: Path) -> None:
    renderer = ScriptedRenderer(approvals=[Approval(allow=False)])
    cfg = ForgeConfig(permissions=PermissionsConfig(ask=["edit_file(*.md)", "apply_patch(*.md)"]))
    ctx = make_ctx(tmp_project, cfg=cfg, renderer=renderer)
    patch = "*** Begin Patch\n*** Add File: notes.md\n+a\n*** End Patch"
    result = await run(ctx, "apply_patch", patch=patch)
    assert result.code == "permission_denied" and len(renderer.approval_requests) == 1
