"""`forge-web user …` creates accounts and reset links from the command line."""

import io
from pathlib import Path

import pytest

from forge_web.cli import main


def forge_web(
    data: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch,
    *args: str, stdin: str = "",
) -> tuple[int, str, str]:  # fmt: skip
    monkeypatch.setattr("sys.stdin", io.StringIO(stdin))
    code = main(["--data-dir", str(data), "user", *args])
    out = capsys.readouterr()
    return code, out.out, out.err


def test_add_list_and_reset_link(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    def run(*args: str, stdin: str = "") -> tuple[int, str, str]:
        return forge_web(tmp_path, capsys, monkeypatch, *args, stdin=stdin)

    code, out, _ = run("add", "--email", "Ann@Example.com", "--admin", "--password-stdin",
                       stdin="a long enough password\n")  # fmt: skip
    assert code == 0 and "created admin ann@example.com" in out
    code, out, _ = run("add", "--email", "bo@example.com")
    assert code == 0 and "/reset#token=" in out
    assert run("add", "--email", "bo@example.com")[0] == 1
    code, _, err = run("add", "--email", "c@example.com", "--password-stdin", stdin="x\n")
    assert code == 1 and "password" in err
    listed = run("list")[1]
    assert "ann@example.com" in listed and "bo@example.com" in listed
    assert "/reset#token=" in run("reset-link", "--email", "ann@example.com")[1]
    assert run("reset-link", "--email", "nobody@example.com")[0] == 1
