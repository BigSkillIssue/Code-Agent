"""What a new node downloads while it installs itself (W64). Both routes are public: the script
holds nothing secret (the join code comes from the admin's command line), and the wheel is the
open-source host worker.

- `GET /install/node.sh`: the node installer with this server's address.
- `GET /install/forge_hostworker.whl`: the host worker's wheel, built into the server image.
"""

from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, PlainTextResponse

from forge_web.hosting.install import render_node_script
from forge_web.services import services_of

# Where server.Dockerfile puts the wheel; outside the image the route answers 404.
WHEELS_DIR = Path("/opt/forge-web/wheels")
WHEEL_GLOB = "forge_hostworker-*.whl"
NO_STORE = {"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"}


def newest_wheel(folder: Path) -> Path | None:
    """The host worker's newest wheel in `folder`, if there is one."""
    if not folder.is_dir():
        return None
    found = [path for path in folder.glob(WHEEL_GLOB) if path.is_file()]
    return max(found, key=lambda path: path.stat().st_mtime, default=None)


def install_router(wheels_dir: Path = WHEELS_DIR) -> APIRouter:
    """The node installer and the wheel it installs."""
    router = APIRouter(prefix="/install")

    @router.get("/node.sh")
    async def node_sh(request: Request) -> PlainTextResponse:
        settings = services_of(request).settings
        try:
            script = render_node_script(settings.base_url(), settings.hosting.apps_domain)
        except ValueError as error:
            raise HTTPException(
                500, "Nodes install themselves only from a plain https:// address: set "
                f"server.public_url to one, e.g. https://forge.example.com ({error})",
            ) from error  # fmt: skip
        return PlainTextResponse(script, media_type="text/x-shellscript", headers=NO_STORE)

    @router.get("/forge_hostworker.whl")
    async def wheel() -> FileResponse:
        path = newest_wheel(wheels_dir)
        if path is None:
            raise HTTPException(404, "this server has no host worker wheel (see server.Dockerfile)")
        return FileResponse(
            path, media_type="application/octet-stream", filename=path.name, headers=NO_STORE
        )

    return router
