"""One session in the browser: sign in, a project, a chat whose edit needs approval, its diff,
the files and changes, a terminal and a live preview."""

import re
from pathlib import Path
from typing import Any

import pytest

from support import LiveServer, free_port

STATIC = Path(__file__).parents[2] / "packages" / "server" / "src" / "forge_web" / "static"

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.skipif(not (STATIC / "index.html").exists(), reason="build the web UI first"),
]


async def sign_in(page: Any, server: LiveServer) -> None:
    await page.goto(f"{server.url}/api/auth/dev-login?token={server.services.dev_token}")
    await page.get_by_text("Projekte", exact=True).wait_for()


async def new_project_and_chat(page: Any, name: str) -> None:
    await page.get_by_title("Neues Projekt").click()
    await page.locator("[role=dialog] input").first.fill(name)
    await page.locator("[role=dialog] button[type=submit]").click()
    await page.wait_for_url(re.compile(r"/p/"))
    await page.get_by_role("button", name="Neuer Chat").last.click()
    await page.locator("textarea").wait_for()


async def test_a_chat_edit_needs_approval_and_shows_its_diff(page: Any, server: LiveServer) -> None:
    await sign_in(page, server)
    await new_project_and_chat(page, "E2E")
    await (
        page.locator("select")
        .filter(has_text="Vor Änderungen fragen")
        .select_option(label="Vor Änderungen fragen")
    )
    await page.locator("textarea").fill("Schreib eine hello.py")
    await page.locator("textarea").press("Enter")
    allow = page.get_by_role("button", name="Erlauben", exact=True)
    await allow.wait_for()
    await allow.click()
    await page.get_by_text('print("hello from e2e")').first.wait_for()  # the diff
    await page.get_by_text("is written").wait_for()  # the model's report
    await page.get_by_title(re.compile("Dateien, Änderungen")).click()
    await page.get_by_role("tab", name="Dateien").click()
    await page.get_by_text("hello.py").last.wait_for()
    await page.get_by_role("tab", name="Änderungen").click()
    await page.locator("[data-testid=changes]").get_by_text("hello.py").wait_for()


async def test_terminal_and_preview(page: Any, server: LiveServer) -> None:
    await sign_in(page, server)
    await new_project_and_chat(page, "Werkzeuge")
    await page.get_by_title(re.compile("Dateien, Änderungen")).click()

    await page.get_by_role("tab", name="Terminal").click()
    await page.locator(".xterm").first.click()
    await page.keyboard.type("echo e2e-$((6*7)) > index.html && echo done-$((1+1))\n")
    await page.locator(".xterm-rows", has_text="done-2").first.wait_for()

    port = free_port()
    await page.get_by_role("tab", name="Vorschau").click()
    await page.get_by_label(re.compile("Eigener Befehl")).fill(
        f"python3 -m http.server {port} --bind 127.0.0.1"
    )
    await page.get_by_role("button", name="Starten").click()
    await page.get_by_role("button", name=f"Port {port} anzeigen").click()
    frame = page.frame_locator("iframe[title=Vorschau]")
    await frame.get_by_text("e2e-42").wait_for()  # the page from the project, on its own host
