"""The eval suite: its shape, per-model reports, and the SWE-bench Lite runner (offline)."""

import json
import os
import subprocess
from collections import Counter
from pathlib import Path

import pytest

from forge.cli import main
from forge.evals import load_tasks
from forge.prompts import PROMPTS_VERSION
from support import init_repo

REPO = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def clean_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    for name in list(os.environ):
        if name.startswith("FORGE_"):
            monkeypatch.delenv(name)
    monkeypatch.setenv("FORGE_HOME", str(tmp_path / "home"))


def test_suite_covers_every_category_offline() -> None:
    tasks = load_tasks(REPO / "evals")
    assert 30 <= len(tasks) <= 50
    categories = Counter(t.category for t in tasks)
    for category in ("bug", "feature", "refactor", "multi-file", "windows", "large"):
        assert categories[category] >= 3, categories
    assert all(t.fake is not None for t in tasks)  # every task runs offline with --fake
    assert len({t.name for t in tasks}) == len(tasks)


def test_models_report_appends_rows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(REPO)
    report = tmp_path / "RESULTS.md"
    report.write_text("# Results\n")
    args = [
        "--fake",
        "eval",
        "--suite",
        "windows",
        "--models",
        "a/one,b/two",
        "--report",
        str(report),
    ]
    assert main(args) == 0
    assert main(args) == 0  # a second run appends
    text = report.read_text()
    assert text.startswith("# Results\n\n| date | model | prompts | suite |")
    rows = [line for line in text.splitlines() if line.startswith("| 20")]
    assert len(rows) == 4
    assert f"| a/one | {PROMPTS_VERSION} | windows | 4/4 | 100% |" in rows[0]
    assert "results added to" in capsys.readouterr().out


TEST_PATCH = """diff --git a/test_calc.py b/test_calc.py
new file mode 100644
--- /dev/null
+++ b/test_calc.py
@@ -0,0 +1,5 @@
+from calc import add
+
+
+def test_add():
+    assert add(2, 3) == 5
"""


def test_swebench_runner_offline(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    upstream = init_repo(tmp_path / "upstream")
    (upstream / "calc.py").write_text("def add(a, b):\n    return a - b\n")
    subprocess.run(["git", "add", "-A"], cwd=upstream, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "start"], cwd=upstream, check=True)
    sha = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=upstream, capture_output=True, text=True, check=True
    ).stdout.strip()
    base = {
        "repo": str(upstream),
        "base_commit": sha,
        "problem_statement": "add() subtracts instead of adding",
        "test_patch": TEST_PATCH,
        "FAIL_TO_PASS": json.dumps(["test_calc.py::test_add"]),
    }
    fixed = {
        **base,
        "instance_id": "demo__calc-1",
        "forge_fake": {"edits": [{"path": "calc.py", "old": "a - b", "new": "a + b"}]},
    }
    unfixed = {**base, "instance_id": "demo__calc-2"}
    data = tmp_path / "lite.jsonl"
    data.write_text(json.dumps(fixed) + "\n" + json.dumps(unfixed) + "\n")
    code = main(["--fake", "eval", "--swebench", str(data)])
    out = capsys.readouterr().out
    assert "pass  demo__calc-1" in out and "FAIL  demo__calc-2" in out
    assert "passed 1/2" in out and code == 1
    assert main(["--fake", "eval", "--swebench", str(data), "--limit", "1"]) == 0


async def test_a_task_whose_models_all_fail_counts_as_failed() -> None:
    """A live run crashed the whole suite when every model was out of quota for one task."""
    from forge.config import ForgeConfig
    from forge.evals import run_eval
    from forge.providers.fake import FakeProvider, FakeTurn
    from forge.wiring import install_fake

    task = load_tasks(REPO / "evals", "fix-add")[0]
    cfg = ForgeConfig()
    out = FakeTurn(error="rate_limit", retry_after_s=4 * 3600)
    install_fake(cfg, FakeProvider([out] * 20))
    result = await run_eval(task, REPO / "evals", cfg, fake=False)
    assert not result.passed and "model error (rate_limit)" in result.note
