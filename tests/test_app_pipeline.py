"""Hosting checkpoints in run_task (S67b): the request and the plan are reviewed, the product is
reviewed only after a green `app check`, with desktop and phone screenshots, and only the
user's "go live" makes it ready to host."""

import argparse
import json
from pathlib import Path
from typing import Any

import pytest

from forge import app_dev
from forge.app_flow import GO_LIVE, NOT_YET, SEND_BACK
from forge.app_template import write_app
from forge.cli import build_parser, load
from forge.config import AppConfig, ForgeConfig
from forge.ctx import Ctx
from forge.pipeline import report_text, run_task
from forge.plan import TaskSpec
from forge.ports import Answer, Browser, Command, CommandResult, PageView
from forge.providers.base import ImagePart
from forge.providers.fake import FakeProvider, FakeToolCall, FakeTurn
from forge.release_review import RELEASE_AREAS
from forge.runtime import postgres
from forge.wiring import install_fake
from support import ScriptedExecutor, ScriptedRenderer, git, init_repo, make_ctx

SHOT = ImagePart(media_type="image/png", data_b64="iVBORw0KGgo=")
SPEC = TaskSpec(
    goal="A shared shopping list",
    context="",
    requirements=["lists can be shared"],
    acceptance_criteria=["a shared list shows for both"],
    size="small",
)
STEPS = [{"title": "Lists", "detail": "a list page", "check": "review: done"}]


class Page:
    """A browser page that shows a screenshot of whatever it opens."""

    def __init__(self, opened: list[str]) -> None:
        self.opened = opened

    async def open(self, url: str) -> PageView:
        self.opened.append(url)
        return PageView(url=url, title="Shopping", image=SHOT)

    async def close(self) -> None:
        """Nothing to close."""


class Preview:
    """Forge's preview browsers: records the sizes it was asked for."""

    def __init__(self) -> None:
        self.viewports: list[tuple[int, int] | None] = []
        self.opened: list[str] = []

    async def new_browser(self, viewport: tuple[int, int] | None = None) -> Browser:
        self.viewports.append(viewport)
        return Page(self.opened)  # type: ignore[return-value]

    async def close(self) -> None:
        """Nothing to stop."""


def verdict(status: str = "ok", summary: str = "") -> FakeTurn:
    """A release reviewer answer: `content` with `status`, the other areas ok."""
    findings = [
        {"area": a, "status": "ok", "reason": "kept"} for a in RELEASE_AREAS if a != "content"
    ]
    findings.append(
        {
            "area": "content",
            "status": status,
            "rule": "DSA Art. 16",
            "reason": "shared lists cannot be reported",
            "fix": "add a report button",
        }
    )
    body = {"summary": summary or f"Verdict {status}.", "findings": findings}
    return FakeTurn(text=json.dumps(body))


@pytest.fixture
def product(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A committed product from the template; PostgreSQL "installed", services "healthy"."""
    root = tmp_path / "shop"
    write_app(root, "shop")
    init_repo(root)
    git(root, "add", "-A")
    git(root, "commit", "-qm", "new product")
    bin_dir = tmp_path / "pgbin"
    bin_dir.mkdir()
    monkeypatch.setattr(postgres, "find_bin_dir", lambda: bin_dir)
    monkeypatch.setattr(postgres.os, "geteuid", lambda: 1000, raising=False)

    async def healthy(manifest: object, timeout_s: float = 0) -> bool:
        return True

    monkeypatch.setattr(app_dev, "wait_healthy", healthy)
    return root


def app_run(
    root: Path,
    reviews: list[FakeTurn],
    *,
    answers: list[list[str]] | None = None,
    executor: ScriptedExecutor | None = None,
    coder_extra: list[FakeTurn] | None = None,
    headless: bool = False,
    review_on: bool = True,
    preview: Preview | None = None,
) -> tuple[Ctx, FakeProvider]:
    plan = FakeToolCall(name="submit_plan", arguments={"steps": STEPS})
    write = FakeToolCall(
        name="write_file", arguments={"path": "server/app/lists.py", "content": "LISTS = []\n"}
    )
    roles: dict[str, list[Any]] = {
        "refiner": [FakeTurn(text=SPEC.model_dump_json())],
        "planner": [FakeTurn(tool_calls=[plan]), FakeTurn(text="planned")],
        "coder": [FakeTurn(tool_calls=[write]), FakeTurn(text="written"), *(coder_extra or [])],
        "reviewer": [
            FakeTurn(text='{"pass": true, "reason": "ok"}'),
            FakeTurn(text='{"ok": true, "summary": "Lists added."}'),
        ],
        "release_reviewer": reviews,
    }
    replies = [[Answer(question_index=0, values=[a]) for a in batch] for batch in answers or []]
    cfg = ForgeConfig(app=AppConfig(review=review_on))
    ctx = make_ctx(
        root,
        cfg=cfg,
        renderer=ScriptedRenderer(answers=replies),
        executor=executor or ScriptedExecutor(),
        headless=headless,
    )
    ctx.state.preview_browsers = preview if preview is not None else Preview()
    fake = install_fake(cfg, FakeProvider(roles=roles))
    return ctx, fake


def requests_of(fake: FakeProvider, role: str) -> list[str]:
    return ["\n".join(m.text() for m in r.messages) for r in fake.requests if r.model == role]


def at_commit(head: str) -> ScriptedExecutor:
    """An executor whose `git rev-parse HEAD` names this commit (and a clean tree)."""

    def answer(cmd: Command) -> CommandResult | None:
        if ScriptedExecutor.words(cmd)[:2] == ["git", "rev-parse"]:
            return CommandResult(exit_code=0, stdout=head + "\n", stderr="")
        return None

    return ScriptedExecutor(answer)


async def test_a_product_goes_live_only_with_the_users_word(product: Path) -> None:
    preview = Preview()
    head = git(product, "rev-parse", "HEAD").strip()
    ctx, fake = app_run(
        product,
        [verdict(), verdict(), verdict()],
        answers=[[GO_LIVE]],
        preview=preview,
        executor=at_commit(head),
    )
    report = await run_task("a shared shopping list", ctx)
    assert report.ok and report.ready_to_host
    assert head[:12] in report.host_summary
    assert "Ready to go live: yes" in report_text(report)
    assert [r.stage for r in ctx.state.release_reviews] == ["prompt", "plan", "product"]
    assert preview.viewports == [(1280, 800), (390, 844)]
    assert preview.opened == ["http://localhost:8080", "http://localhost:8080"]
    product_request = [r for r in fake.requests if r.model == "release_reviewer"][-1]
    images = [p for m in product_request.messages for p in m.parts if isinstance(p, ImagePart)]
    assert len(images) == 2
    assert "app check: passed" in requests_of(fake, "release_reviewer")[-1]
    out = product / ".forge" / "out" / "app"
    assert {"checks.json", "approval.json", "desktop.png", "phone.png"} <= {
        p.name for p in out.iterdir()
    }
    assert json.loads((out / "approval.json").read_text(encoding="utf-8"))["commit"] == head


async def test_red_checks_skip_the_review_and_go_back_to_the_agent(product: Path) -> None:
    runs = {"npm test": 0}

    def answer(cmd: Command) -> CommandResult | None:
        if ScriptedExecutor.words(cmd)[:2] == ["npm", "test"]:
            runs["npm test"] += 1
            if runs["npm test"] == 1:
                return CommandResult(exit_code=1, stdout="1 test failed: Layout", stderr="")
        return None

    fix = [FakeTurn(text="fixed the layout test")]
    ctx, fake = app_run(
        product,
        [verdict(), verdict(), verdict()],
        answers=[[GO_LIVE]],
        executor=ScriptedExecutor(answer),
        coder_extra=fix,
    )
    report = await run_task("a shared shopping list", ctx)
    assert report.ready_to_host
    assert len(requests_of(fake, "release_reviewer")) == 3  # the product was reviewed once
    coder = requests_of(fake, "coder")[-1]
    assert "1 test failed: Layout" in coder and "app_check" in coder


async def test_a_violation_goes_back_to_the_agent_first(product: Path) -> None:
    fix = [FakeTurn(text="added the report button")]
    ctx, fake = app_run(
        product,
        [verdict(), verdict(), verdict("violation"), verdict()],
        answers=[[GO_LIVE]],
        coder_extra=fix,
    )
    report = await run_task("a shared shopping list", ctx)
    assert report.ready_to_host
    assert "add a report button" in requests_of(fake, "coder")[-1]


async def test_send_back_continues_with_the_users_note(product: Path) -> None:
    extra = [FakeTurn(text="made the button blue")]
    ctx, fake = app_run(
        product,
        [verdict(), verdict(), verdict(), verdict()],
        answers=[[SEND_BACK], ["make the share button blue"], [GO_LIVE]],
        coder_extra=extra,
    )
    report = await run_task("a shared shopping list", ctx)
    assert report.ready_to_host
    assert "make the share button blue" in requests_of(fake, "coder")[-1]
    assert len(requests_of(fake, "release_reviewer")) == 4  # the product was reviewed again


async def test_not_yet_ends_without_going_live(product: Path) -> None:
    ctx, _ = app_run(product, [verdict(), verdict(), verdict()], answers=[[NOT_YET]])
    report = await run_task("a shared shopping list", ctx)
    assert report.ok and not report.ready_to_host
    assert "Ready to go live: no" in report_text(report)
    assert not (product / ".forge" / "out" / "app" / "approval.json").exists()


async def test_headless_runs_never_approve(product: Path) -> None:
    ctx, _ = app_run(product, [verdict(), verdict(), verdict()], headless=True)
    report = await run_task("a shared shopping list", ctx)
    assert not report.ready_to_host


async def test_a_request_that_breaks_the_rules_stops_before_planning(product: Path) -> None:
    ctx, fake = app_run(product, [verdict("violation", summary="Nobody can report lists.")])
    report = await run_task("a shared shopping list", ctx)
    assert not report.ok and not report.ready_to_host
    assert "release review of the prompt" in report.summary
    assert not requests_of(fake, "refiner") and not requests_of(fake, "planner")


async def test_without_a_browser_the_reviewer_is_told(product: Path) -> None:
    ctx, fake = app_run(product, [verdict(), verdict(), verdict()], answers=[[NOT_YET]])
    ctx.state.preview_browsers = None
    await run_task("a shared shopping list", ctx)
    assert "There are no screenshots" in requests_of(fake, "release_reviewer")[-1]


async def test_without_the_app_option_nothing_is_reviewed(product: Path) -> None:
    ctx, fake = app_run(product, [], review_on=False)
    report = await run_task("a shared shopping list", ctx)
    assert report.ok and not report.ready_to_host and not report.host_summary
    assert not requests_of(fake, "release_reviewer")


def test_the_app_flag_turns_the_review_on(tmp_project: Path) -> None:
    options: argparse.Namespace = build_parser().parse_args(["-C", str(tmp_project), "--app"])
    options.fake = None
    _, cfg = load(options)
    assert cfg.app.review
