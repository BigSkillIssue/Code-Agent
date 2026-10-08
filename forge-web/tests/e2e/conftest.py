"""A real Forge Web server with the fake model, and Chromium driving its web UI.

Local isolation by default; FORGE_WEB_E2E_ISOLATION=docker runs every project in a container
(with the image in FORGE_WEB_TEST_IMAGE). Chromium comes from FORGE_WEB_E2E_CHROMIUM, the
preinstalled browsers in /opt/pw-browsers, or `playwright install chromium`.
"""

import glob
import os
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any

import pytest

from support import (
    LiveServer,
    call,
    dev_settings,
    fake_script,
    free_port,
    remove_docker_projects,
)

STATIC = Path(__file__).parents[2] / "packages" / "server" / "src" / "forge_web" / "static"
HELLO = 'print("hello from e2e")\n'


def script() -> dict[str, Any]:
    """The fake model writes one file (which needs approval in ask mode), then reports."""
    return fake_script(call("write_file", path="hello.py", content=HELLO),
                       {"text": "Done: **hello.py** is written."})  # fmt: skip


@pytest.fixture
def server(tmp_path: Path) -> Iterator[LiveServer]:
    isolation = os.environ.get("FORGE_WEB_E2E_ISOLATION", "local")
    image = os.environ.get("FORGE_WEB_TEST_IMAGE") if isolation == "docker" else None
    settings = dev_settings(tmp_path / "data", script(), isolation=isolation, image=image)
    port = free_port()
    settings.server.port = port  # preview URLs and Forge's frame-src use the real port
    with LiveServer(settings, port=port) as live:
        yield live
    if isolation == "docker":
        remove_docker_projects(tmp_path / "data")  # the server leaves containers running


def chromium_path() -> str | None:
    if os.environ.get("FORGE_WEB_E2E_CHROMIUM"):
        return os.environ["FORGE_WEB_E2E_CHROMIUM"]
    found = sorted(glob.glob("/opt/pw-browsers/chromium-*/chrome-linux/chrome"))
    return found[-1] if found else None


@pytest.fixture
async def page(server: LiveServer) -> AsyncIterator[Any]:
    from playwright.async_api import async_playwright

    async with async_playwright() as p:
        browser = await p.chromium.launch(executable_path=chromium_path())
        context = await browser.new_context(locale="de-DE", viewport={"width": 1400, "height": 900})
        tab = await context.new_page()
        tab.set_default_timeout(30_000)
        yield tab
        await browser.close()
