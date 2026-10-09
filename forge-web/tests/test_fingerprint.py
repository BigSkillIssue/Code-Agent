"""Which Forge runs where: the fingerprint the server, each sandbox and the doctor compare."""

from pathlib import Path

import forge
import pytest

from forge_sandbox.cli import main
from forge_sandbox.fingerprint import fingerprint_of, forge_fingerprint


def package(root: Path, files: dict[str, str]) -> Path:
    for name, text in files.items():
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_text(text)
    return root


def test_the_fingerprint_follows_the_python_files_only(tmp_path: Path) -> None:
    first = package(tmp_path / "a", {"__init__.py": "", "tools.py": "X = 1\n"})
    same = package(tmp_path / "b", {"__init__.py": "", "tools.py": "X = 1\n"})
    assert fingerprint_of(first) == fingerprint_of(same) and len(fingerprint_of(first)) == 12
    package(same, {"__pycache__/tools.cpython-312.pyc": "bytes", "README.md": "words"})
    assert fingerprint_of(first) == fingerprint_of(same)  # caches and data files do not count
    package(same, {"tools.py": "X = 2\n"})
    assert fingerprint_of(first) != fingerprint_of(same)
    moved = package(tmp_path / "c", {"__init__.py": "", "sub/tools.py": "X = 1\n"})
    assert fingerprint_of(first) != fingerprint_of(moved)


def test_forge_fingerprint_is_the_imported_forge(capsys: pytest.CaptureFixture[str]) -> None:
    assert forge_fingerprint() == fingerprint_of(Path(forge.__file__).parent)
    assert main(["fingerprint"]) == 0
    assert capsys.readouterr().out.strip() == forge_fingerprint()
