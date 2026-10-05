"""Architecture rules from AGENTS.md, checked by reading the source with `ast`."""

import ast
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src" / "forge"

CORE = ["pipeline.py", "agent.py", "team.py", "compress.py", "tools.py"]
FORBIDDEN_IN_CORE = ("textual", "rich", "sqlite3", "sqlalchemy", "subprocess", "forge.local")

# Module-level imports must point inward. Keys are module paths (relative to src/forge),
# values are forge modules they must not import at load time.
UPWARD = {
    "providers/": ("forge.tools", "forge.agent", "forge.team", "forge.pipeline", "forge.cli"),
    "runtime/": ("forge.tools", "forge.agent", "forge.team", "forge.pipeline", "forge.cli"),
    "tools.py": ("forge.agent", "forge.team", "forge.pipeline", "forge.cli", "forge.tui"),
    "agent.py": ("forge.team", "forge.pipeline", "forge.cli", "forge.tui", "forge.api"),
    "pipeline.py": ("forge.cli", "forge.tui", "forge.api"),
}


def imported_modules(tree: ast.AST, top_level_only: bool) -> list[str]:
    """Every module named by an import statement; optionally only module-level ones."""
    nodes = tree.body if top_level_only and isinstance(tree, ast.Module) else ast.walk(tree)
    names: list[str] = []
    for node in nodes:
        if isinstance(node, ast.Import):
            names.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.append(node.module)
            names.extend(f"{node.module}.{alias.name}" for alias in node.names)
    return names


def matches(name: str, banned: str) -> bool:
    return name == banned or name.startswith(banned + ".")


@pytest.mark.parametrize("module", CORE)
def test_core_imports_only_ports(module: str) -> None:
    path = SRC / module
    if not path.exists():
        pytest.skip(f"{module} is built in a later step")
    names = imported_modules(ast.parse(path.read_text(encoding="utf-8")), top_level_only=False)
    bad = sorted({n for n in names for b in FORBIDDEN_IN_CORE if matches(n, b)})
    assert not bad, f"{module} imports {bad}; core modules must receive these through Ctx"


def test_module_level_imports_point_inward() -> None:
    problems = []
    for path in sorted(SRC.rglob("*.py")):
        rel = path.relative_to(SRC).as_posix()
        banned = next((v for k, v in UPWARD.items() if rel == k or rel.startswith(k)), ())
        names = imported_modules(ast.parse(path.read_text(encoding="utf-8")), True)
        problems += [f"{rel} imports {n}" for n in names for b in banned if matches(n, b)]
    assert not problems, "upward imports:\n" + "\n".join(problems)


def test_checker_catches_a_forbidden_import() -> None:
    tree = ast.parse(
        "def f():\n    import subprocess\nfrom forge.local.memory_bus import MemoryBus\n"
    )
    names = imported_modules(tree, top_level_only=False)
    assert any(matches(n, "subprocess") for n in names)
    assert any(matches(n, "forge.local") for n in names)
    assert not any(matches(n, "subprocess") for n in imported_modules(tree, True))
