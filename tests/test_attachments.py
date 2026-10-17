"""Files and images attached to a task's message (S75)."""

import base64
import json
from pathlib import Path

from forge import prompts
from forge.cli import build_parser
from forge.config import ForgeConfig
from forge.ctx import Ctx
from forge.pipeline import run_task
from forge.providers.base import Capabilities, ImagePart, Message
from forge.providers.fake import FakeProvider, FakeTurn
from forge.providers.registry import register_provider
from forge.runtime.attachments import MAX_ATTACHMENTS, Attachment, attachment_for
from support import make_ctx

# The smallest valid PNG: one transparent pixel.
PIXEL = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg=="
)
TRIVIAL = json.dumps(
    {"goal": "describe the files", "context": "", "requirements": [],
     "acceptance_criteria": ["answered"], "size": "trivial"}
)  # fmt: skip


def run_ctx(root: Path, vision: bool = True) -> tuple[Ctx, FakeProvider]:
    """A ctx whose refiner says the task is trivial and whose coder just answers."""
    caps = Capabilities(context_window=128_000, max_output=8_192, vision=vision)
    fake = FakeProvider(
        roles={"refiner": [FakeTurn(text=TRIVIAL)], "coder": [FakeTurn(text="a red dot")]},
        caps=caps,
    )
    cfg = ForgeConfig(roles={"refiner": ["fake/refiner"], "coder": ["fake/coder"]})
    register_provider(cfg, fake)
    return make_ctx(root, cfg=cfg, headless=True), fake


def files(root: Path) -> list[Attachment]:
    """A screenshot and a PDF, attached as a server would: by path, name and type."""
    (root / ".forge" / "attachments").mkdir(parents=True)
    (root / ".forge" / "attachments" / "shot.png").write_bytes(PIXEL)
    (root / "spec.pdf").write_bytes(b"%PDF-1.4 the spec")
    return [
        Attachment(path=".forge/attachments/shot.png", name="Screenshot 1.png"),
        Attachment(path="spec.pdf", name="spec.pdf", media_type="application/pdf"),
    ]


def first_user(message_lists: list[list[Message]]) -> Message:
    return message_lists[0][0]


async def test_an_image_reaches_the_model_as_an_image_and_a_pdf_by_its_path(
    tmp_project: Path,
) -> None:
    ctx, fake = run_ctx(tmp_project)
    report = await run_task("What is on the screenshot?", ctx, attachments=files(tmp_project))
    assert report.ok, report.summary
    refiner, coder = fake.requests
    for request in (refiner, coder):
        [image] = [p for m in request.messages for p in m.parts if isinstance(p, ImagePart)]
        assert (image.media_type, base64.b64decode(image.data_b64)) == ("image/png", PIXEL)
    text = refiner.messages[0].text()
    assert "What is on the screenshot?" in text
    assert ".forge/attachments/shot.png" in text and "Screenshot 1.png" in text
    assert "spec.pdf (spec.pdf, application/pdf, 17 B)" in text
    assert "spec.pdf" in coder.system  # every agent of the task sees the list
    assert ctx.state.attachments == []  # the next task starts without them


async def test_a_model_that_cannot_see_gets_the_list_only(tmp_project: Path) -> None:
    ctx, fake = run_ctx(tmp_project, vision=False)
    await run_task("What is on the screenshot?", ctx, attachments=files(tmp_project))
    for request in fake.requests:
        assert not any(isinstance(p, ImagePart) for m in request.messages for p in m.parts)
    text = fake.requests[0].messages[0].text()
    assert ".forge/attachments/shot.png" in text and "read_file" in text


async def test_too_many_or_missing_attachments_stop_the_task_cleanly(tmp_project: Path) -> None:
    ctx, fake = run_ctx(tmp_project)
    missing = await run_task("look", ctx, attachments=[Attachment(path="nope.png")])
    assert not missing.ok and "nope.png" in missing.summary
    (tmp_project / "a.txt").write_text("a", encoding="utf-8")
    many = [Attachment(path="a.txt")] * (MAX_ATTACHMENTS + 1)
    too_many = await run_task("look", ctx, attachments=many)
    assert not too_many.ok and str(MAX_ATTACHMENTS) in too_many.summary
    assert fake.requests == []  # no model was asked


def test_the_cli_attaches_files_and_the_prompt_explains_them(tmp_project: Path) -> None:
    (tmp_project / "notes.md").write_text("# notes\n", encoding="utf-8")
    found = attachment_for(tmp_project, "notes.md")
    assert (found.name, found.media_type, found.size) == ("notes.md", "text/markdown", 8)
    options, rest = build_parser().parse_known_args(
        ["--attach", "notes.md", "--attach", "shot.png", "fix", "it"]
    )
    assert options.attach == ["notes.md", "shot.png"] and rest == ["fix", "it"]
    assert "read_file" in prompts.render("attached", files="- notes.md")
