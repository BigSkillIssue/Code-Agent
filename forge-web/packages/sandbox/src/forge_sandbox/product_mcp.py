"""The product tools Forge's agent gets in Forge Web (W54), as an MCP server Forge starts in the
sandbox (`python -m forge_sandbox.product_mcp`). In Forge they are `mcp__forge_web__<tool>`.

- product_create / product_add: place a product (W51) in the project, or add app clients and
  payments to it; what someone changed is never overwritten, it comes back as a diff to merge.
- product_look: a screenshot of a page of the product (its dev server here, or its own hosted
  addresses), at a phone or desk width, light or dark.
- deploy, deploy_status, app_logs, app_fetch, app_sql, android_build, windows_build: the product
  on Forge's servers, through the gateway with the chat's run token; the server limits every call
  to this project's own app.
"""

import argparse
import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit

import httpx
from forge.app_manifest import load_manifest
from mcp.server.mcpserver import Image, MCPServer

from forge_sandbox.products.place import Placed, add, create
from forge_sandbox.products.render import ProductError

LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}
Environment = Literal["staging", "production"]


class Tools:
    """The tools' state: the project, the gateway and the apps domain."""

    def __init__(self, root: Path, gateway: str, token_env: str, apps_domain: str) -> None:
        self.root = root
        self.gateway = gateway.rstrip("/")
        self.token_env = token_env
        self.apps_domain = apps_domain

    def placed(self, done: Placed) -> str:
        """What placing did, for the agent."""
        lines = [f"written: {len(done.written)} files", f"already right: {len(done.same)} files"]
        if done.to_merge:
            lines.append("these files differ from the template; merge the template's change "
                         "yourself (diffs below):")  # fmt: skip
            lines += [f"\n{diff}" for diff in done.to_merge.values()]
        return "\n".join(lines)

    def own_hosts(self) -> set[str]:
        """The product's own addresses on Forge's servers."""
        manifest = load_manifest(self.root / "forge.app.toml")
        if isinstance(manifest, list) or not self.apps_domain:
            return set()
        name, domain = manifest.name, self.apps_domain
        return {f"{name}.{domain}", f"{name}.staging.{domain}"}

    def head(self) -> str:
        """The commit to deploy or build: HEAD, which must hold every change."""
        status = subprocess.run(["git", "status", "--porcelain"], cwd=self.root,
                                capture_output=True, text=True, check=False)  # fmt: skip
        if status.stdout.strip():
            raise ProductError("commit your changes first: only committed work is deployed")
        found = subprocess.run(["git", "rev-parse", "HEAD"], cwd=self.root,
                               capture_output=True, text=True, check=False)  # fmt: skip
        if found.returncode != 0:
            raise ProductError("the project has no commit yet")
        return found.stdout.strip()

    async def call(self, method: str, path: str, body: dict[str, Any] | None = None) -> str:
        """A gateway call; its answer as indented JSON (errors too, for the agent to read)."""
        if not self.gateway:
            return "this sandbox has no connection to Forge Web's servers"
        headers = {"Authorization": f"Bearer {os.environ.get(self.token_env, '')}"}
        try:
            async with httpx.AsyncClient(base_url=self.gateway, timeout=180) as client:
                response = await client.request(method, path, json=body, headers=headers)
            data = response.json()
        except (httpx.HTTPError, ValueError) as err:
            return f"Forge Web could not be reached: {err}"
        if isinstance(data, dict) and "detail" in data:
            data = {"error": data["detail"]}
        return json.dumps(data, indent=2, ensure_ascii=False)[:120_000]


def server(tools: Tools) -> MCPServer:
    """The MCP server with every product tool."""
    mcp: MCPServer = MCPServer("forge_web", instructions=(
        "Your product on Forge Web: create it, add apps, look at its pages, deploy it to "
        "staging, read its logs and database, build its apps."))  # fmt: skip

    @mcp.tool()
    def product_create(kind: Literal["fullstack", "site"], name: str, title: str = "",
                       features: list[str] | None = None) -> str:  # fmt: skip
        """Make this project a product. kind "fullstack": a server (FastAPI, PostgreSQL) with
        accounts and a web client; "site": a website without a server. name: lowercase slug (its
        address). features (fullstack only): payments, apple, android, windows, digital_goods."""
        try:
            done = create(tools.root, kind, name, title or None, features or [], tools.apps_domain)
        except ProductError as err:
            return f"error: {err}"
        return tools.placed(done)

    @mcp.tool()
    def product_add(features: list[str], title: str = "") -> str:
        """Add app clients or features to this product: apple, android, windows, payments,
        digital_goods. Files nobody changed are updated; changed ones come back as diffs."""
        try:
            done = add(tools.root, title or None, features, tools.apps_domain)
        except ProductError as err:
            return f"error: {err}"
        return tools.placed(done)

    @mcp.tool()
    async def product_look(url: str, width: int = 1280, height: int = 800,
                           dark: bool = False) -> Image | str:  # fmt: skip
        """A screenshot of a page of this product: its dev server (http://localhost:PORT/...)
        or its own hosted addresses. Use width 390 for a phone."""
        host = urlsplit(url).hostname or ""
        if urlsplit(url).scheme not in ("http", "https") or (
            host not in LOCAL_HOSTS and host not in tools.own_hosts()
        ):
            return "error: only this product's own pages (localhost or its hosted addresses)"
        width, height = max(320, min(width, 2560)), max(320, min(height, 2560))
        try:
            return Image(data=await screenshot(url, width, height, dark), format="png")
        except Exception as err:  # the browser's own errors are many; the agent reads them
            return f"error: the page could not be shown: {err}"

    @mcp.tool()
    async def deploy(environment: Environment = "staging") -> str:
        """Deploy the committed HEAD to Forge's servers. Staging first; production only where
        the admin allows it and the same commit runs in staging."""
        try:
            commit = tools.head()
        except ProductError as err:
            return f"error: {err}"
        return await tools.call("POST", "/product/deploy",
                                {"environment": environment, "commit": commit})  # fmt: skip

    @mcp.tool()
    async def deploy_status() -> str:
        """Where the product runs and how its deploys went (steps, errors, addresses)."""
        return await tools.call("GET", "/product/status")

    @mcp.tool()
    async def app_logs(environment: Environment = "staging") -> str:
        """The end of the hosted product's logs, service by service."""
        return await tools.call("POST", "/product/logs", {"environment": environment})

    @mcp.tool()
    async def app_fetch(path: str = "/", environment: Environment = "staging") -> str:
        """A page or API route of the hosted product as a visitor gets it."""
        return await tools.call("POST", "/product/fetch",
                                {"environment": environment, "path": path})  # fmt: skip

    @mcp.tool()
    async def app_sql(query: str, environment: Environment = "staging",
                      write: bool = False) -> str:  # fmt: skip
        """SQL on the hosted product's own database (CSV answer). Read-only unless write;
        schema changes belong in migrations, not here."""
        body = {"environment": environment, "query": query, "write": write}
        return await tools.call("POST", "/product/sql", body)

    @mcp.tool()
    async def store_guidelines(store: Literal["apple", "google_play"]) -> str:
        """The store's review guidelines as the admin loaded them: read them all before a store
        review."""
        return await tools.call("GET", f"/product/store/guidelines/{store}")

    @mcp.tool()
    async def store_review_submit(
        store: Literal["apple", "google_play"],
        verdict: Literal["pass", "concern", "violation"],
        summary: str,
        findings: list[dict[str, str]] | None = None,
    ) -> str:
        """Record the store review of the committed HEAD (the store-review agent's job):
        findings as {"guideline", "status", "reason", "fix"}. Publishing needs "pass"."""
        try:
            commit = tools.head()
        except ProductError as err:
            return f"error: {err}"
        body = {"store": store, "commit": commit, "verdict": verdict, "summary": summary,
                "findings": findings or []}  # fmt: skip
        return await tools.call("POST", "/product/store/review", body)

    @mcp.tool()
    async def store_publish(
        store: Literal["apple", "google_play"],
        platforms: list[Literal["ios", "macos", "watchos"]] | None = None,
        track: Literal["internal", "alpha", "beta", "production"] = "internal",
        notes: str = "",
    ) -> str:
        """Publish the committed HEAD through the store's API once its store review passed:
        Apple to TestFlight (platforms), Google Play to a track (its Android build first)."""
        try:
            commit = tools.head()
        except ProductError as err:
            return f"error: {err}"
        body = {"store": store, "commit": commit, "platforms": platforms or ["ios"],
                "track": track, "notes": notes}  # fmt: skip
        return await tools.call("POST", "/product/store/publish", body)

    @mcp.tool()
    async def android_build() -> str:
        """Build the Android app of the committed HEAD on Forge's servers."""
        return await build(tools, "android")

    @mcp.tool()
    async def windows_build() -> str:
        """Build and pack the Windows app of the committed HEAD on Forge's Windows machines."""
        return await build(tools, "windows")

    return mcp


async def build(tools: Tools, platform: str) -> str:
    """Start an app build of HEAD."""
    try:
        commit = tools.head()
    except ProductError as err:
        return f"error: {err}"
    return await tools.call("POST", f"/product/build/{platform}", {"commit": commit})


async def screenshot(url: str, width: int, height: int, dark: bool) -> bytes:
    """A PNG of the page, with the sandbox's Chromium."""
    from playwright.async_api import async_playwright

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        try:
            scheme = "dark" if dark else "light"
            viewport = {"width": width, "height": height}
            context = await browser.new_context(viewport=viewport, color_scheme=scheme)  # type: ignore[arg-type]
            page = await context.new_page()
            await page.goto(url, wait_until="networkidle", timeout=30_000)
            return await page.screenshot(full_page=True)
        finally:
            await browser.close()


def main(argv: list[str] | None = None) -> None:
    """Run the server on stdin/stdout."""
    parser = argparse.ArgumentParser(prog="forge_sandbox.product_mcp")
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--gateway", default="")
    parser.add_argument("--token-env", default="FW_GATEWAY_TOKEN")
    parser.add_argument("--apps-domain", default="")
    args = parser.parse_args(argv)
    tools = Tools(args.root.resolve(), args.gateway, args.token_env, args.apps_domain)
    asyncio.run(server(tools).run_stdio_async())


if __name__ == "__main__":
    main(sys.argv[1:])
