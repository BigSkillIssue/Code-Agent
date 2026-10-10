# PyInstaller spec for the single-file `forge` binary.
# Build from the repository root: uvx --from pyinstaller pyinstaller packaging/pyinstaller.spec
# Forge loads providers, renderers and stores lazily, so their modules and the data files of
# the libraries behind them are collected explicitly.
from pathlib import Path

from PyInstaller.utils.hooks import collect_all, collect_data_files, collect_submodules

ROOT = Path(SPECPATH).parent
SKIP = ("litellm.proxy", "mcp.cli", "mcp.server")  # server and CLI parts need extras Forge does not use


def wanted(name):
    return not name.startswith(SKIP)


datas, binaries, hiddenimports = [], [], collect_submodules("forge")
datas += collect_data_files("forge", includes=["templates/**/*.tmpl"])  # forge app new
hiddenimports += ["aiosqlite", "sqlalchemy.dialects.sqlite.aiosqlite"]  # loaded by URL scheme
for package in ("textual", "tiktoken_ext", "tree_sitter_language_pack", "playwright"):
    package_datas, package_binaries, package_imports = collect_all(package)
    datas += package_datas
    binaries += package_binaries
    hiddenimports += package_imports
for package in ("litellm", "mcp"):
    datas += collect_data_files(package)
    hiddenimports += collect_submodules(package, filter=wanted)

analysis = Analysis(
    [str(ROOT / "src" / "forge" / "__main__.py")],
    pathex=[str(ROOT / "src")],
    datas=datas,
    binaries=binaries,
    hiddenimports=hiddenimports,
    excludes=["pytest", "mypy", "ruff"],
)
pyz = PYZ(analysis.pure)
exe = EXE(
    pyz,
    analysis.scripts,
    analysis.binaries,
    analysis.datas,
    name="forge",
    console=True,
    upx=False,
)
