"""The server hands out the built web UI, falls back to index.html for app routes, and adds
safe headers; API paths never fall back."""

from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI

from forge_web.webui import SecurityHeaders, mount_web_ui, static_file


@pytest.fixture
def built(tmp_path: Path) -> Path:
    root = tmp_path / "static"
    (root / "assets").mkdir(parents=True)
    (root / "index.html").write_text("<!doctype html><div id=root></div>")
    (root / "assets" / "app-123.js").write_text("console.log(1)")
    (tmp_path / "secret.txt").write_text("outside")
    return root


def client_for(root: Path) -> httpx.AsyncClient:
    app = FastAPI()

    @app.get("/api/health")
    async def health() -> dict[str, bool]:
        return {"ok": True}

    app.add_middleware(SecurityHeaders, https=False)
    mount_web_ui(app, root)
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


async def test_files_index_fallback_and_api_404(built: Path) -> None:
    async with client_for(built) as client:
        asset = await client.get("/assets/app-123.js")
        assert asset.status_code == 200 and "immutable" in asset.headers["cache-control"]
        for route in ("/", "/c/abc", "/p/x/y"):
            page = await client.get(route)
            assert page.status_code == 200 and "<div id=root>" in page.text
        assert (await client.get("/api/health")).json() == {"ok": True}
        assert (await client.get("/api/nope")).status_code == 404
        assert (await client.get("/api")).status_code == 404


async def test_security_headers(built: Path) -> None:
    async with client_for(built) as client:
        page = await client.get("/")
    csp = page.headers["content-security-policy"]
    assert "script-src 'self'" in csp and "frame-ancestors 'none'" in csp
    assert page.headers["x-frame-options"] == "DENY"
    assert page.headers["x-content-type-options"] == "nosniff"


def test_no_path_leaves_the_static_folder(built: Path) -> None:
    assert static_file("../secret.txt", built) is None
    assert static_file("assets/../../secret.txt", built) is None
    assert static_file("assets/app-123.js", built) is not None


async def test_a_missing_build_explains_how_to_build(tmp_path: Path) -> None:
    async with client_for(tmp_path / "nothing") as client:
        page = await client.get("/")
    assert "npm run build" in page.text
